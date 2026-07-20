"""执行单次持久化请求的无界面 CLI Host 编排。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from phi.bootstrap import HostRuntime, model_config_from_settings
from phi.cli.model_selection import (
    require_explicit_model_id,
    resolve_available_model,
)
from phi.harness import EventEmitter, RunEvent, RunResult
from phi.sessions import SessionHandle, create_session, resume_session, select_model, send_message


@dataclass(frozen=True)
class HeadlessOutcome:
    """汇总 Run 结束后的最新 Session handle 与 Harness 结果。"""

    session: SessionHandle
    result: RunResult

    @property
    def session_id(self) -> str:
        """提供结果所属 Session 的便捷标识。"""

        return self.session.session_id


type RuntimeFactory = Callable[[Path], Awaitable[HostRuntime]]
type TextReporter = Callable[[str], object]


async def execute_headless_run(
    task: str,
    *,
    cwd: Path,
    runtime_factory: RuntimeFactory,
    session_id: str | None,
    selected_model: str | None,
    max_steps: int,
    session_handle: SessionHandle | None = None,
    close_runtime: bool = True,
    events: EventEmitter[RunEvent] | None = None,
    report_session: TextReporter | None = None,
    report_diagnostic: TextReporter | None = None,
) -> HeadlessOutcome:
    """解析一个持久化 Session，并把一次 bounded request 委托给共享服务。

    拥有更长 Host 生命周期的调用方可以传入当前不可变 Session handle，并推迟关闭
    runtime；普通 CLI 路径则只拥有单个 Run，并在结束后关闭 runtime。
    """

    # 先验证纯输入，避免为明显无效的命令构建昂贵的 cwd 级运行时资源。
    if not task.strip():
        raise ValueError("TASK must contain non-whitespace text")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")
    if session_id is not None and session_handle is not None:
        raise ValueError("session_id and session_handle are mutually exclusive")

    runtime = await runtime_factory(cwd)
    if not isinstance(runtime, HostRuntime):
        raise TypeError("runtime factory must return HostRuntime")
    try:
        # 不同层可能报告同一诊断；Host 只负责去重和转交渲染器。
        reported_diagnostics: set[str] = set()

        def report_new_diagnostics(diagnostics: tuple[object, ...]) -> None:
            """只向 CLI 报告本次运行尚未显示的诊断。"""

            if report_diagnostic is None:
                return
            for diagnostic in diagnostics:
                text = str(diagnostic)
                if text not in reported_diagnostics:
                    reported_diagnostics.add(text)
                    report_diagnostic(text)

        # 配置校验和资源诊断先于 Session/Run，失败时不会产生新的对话 Entry。
        model_config_from_settings(runtime.settings)
        report_new_diagnostics(runtime.resources.diagnostics)

        # 调用方 handle、显式 Session ID 和创建新 Session 是互斥的三条入口。
        handle = session_handle
        if handle is None and session_id is not None:
            handle = await resume_session(runtime.storage, session_id)
        if handle is not None:
            report_new_diagnostics(handle.diagnostics)
        # Model 优先级是显式 --model、恢复分支 Model、运行时默认 Model。
        explicit_model = (
            None if selected_model is None else require_explicit_model_id(selected_model)
        )
        resumed_model = None if handle is None else handle.metadata.model
        effective_model, model_info = resolve_available_model(
            explicit_model,
            resumed_model,
            runtime.settings.default_model,
            available_models=runtime.available_models,
            missing_message=("no Model was selected; configure PHI_DEFAULT_MODEL or pass --model"),
        )

        # 共享 Session 服务返回新的不可变 handle；Host 不直接修改元数据。
        if handle is None:
            handle = await create_session(runtime.storage, model=effective_model)
        elif explicit_model is not None or handle.metadata.model is None:
            handle = await select_model(runtime.storage, handle, effective_model)

        if report_session is not None:
            report_session(handle.session_id)
        # Context、Harness Run、Tool 与持久化均由 send_message 组合，CLI 只注入观察者。
        handle, result = await send_message(
            handle,
            task,
            storage=runtime.storage,
            settings=runtime.settings,
            model=runtime.model,
            model_info=model_info,
            tools=runtime.resources.tools,
            dispatcher=runtime.resources.dispatcher,
            stable_instructions=runtime.resources.stable_instructions,
            max_steps=max_steps,
            events=events,
            lifecycle=runtime.resources.agents,
        )
        report_new_diagnostics(handle.diagnostics)
        return HeadlessOutcome(handle, result)
    finally:
        # 普通 CLI 必须在所有成功或失败路径关闭 Model/MCP/Subagent 等异步资源。
        if close_runtime:
            await runtime.close()
