"""Phi 的 Typer Host：解析命令并调用共享应用服务。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal, Never

import typer

from phi.bootstrap import build_headless_runtime, model_config_from_settings
from phi.cli.headless import execute_headless_run
from phi.cli.management import (
    execute_context_inspection,
    select_context_session,
)
from phi.cli.model_selection import require_available_model, require_explicit_model_id
from phi.cli.rendering import (
    doctor_progress,
    render_cancelled,
    render_confirmation,
    render_context,
    render_doctor,
    render_empty_sessions,
    render_error,
    render_exhausted,
    render_failure,
    render_mcp_servers,
    render_session_fork,
    render_sessions,
    render_warning,
)
from phi.doctor import (
    DoctorDependencies,
    probe_default_model,
    probe_mcp_servers,
    run_doctor,
)
from phi.harness import RunEvent, RunStatus
from phi.mcp import add_mcp_server, list_configured_mcp_servers, remove_mcp_server
from phi.model import ModelConfig, ModelInfo, list_available_models
from phi.sessions import (
    SessionHandle,
    SessionStorage,
    fork_session,
    list_session_handles,
    redact_text,
    resume_session,
    serialize_run_event,
)
from phi.settings import Settings
from phi.ui import run as run_tui

app = typer.Typer(help="phi — an inspectable Agent Harness.")
session_app = typer.Typer(help="Inspect and branch durable Sessions.")
mcp_app = typer.Typer(help="Manage stdio MCP configuration sources.")
app.add_typer(session_app, name="session")
app.add_typer(mcp_app, name="mcp")
_runtime_factory = build_headless_runtime
_settings_factory: Callable[[], Settings] = Settings
_model_discovery: Callable[[ModelConfig], Coroutine[Any, Any, list[ModelInfo]]] = (
    list_available_models
)
_model_probe = probe_default_model
_mcp_probe = probe_mcp_servers


def _global_mcp_path() -> Path:
    """返回用户级 MCP 配置文件的展开路径。"""

    return Path("~/.phi/mcp.json").expanduser()


def _project_mcp_path(cwd: Path) -> Path:
    """返回指定工作区内的项目级 MCP 配置路径。"""

    return cwd / ".phi" / "mcp.json"


class _JsonlEventWriter:
    """把 Run Event 逐条编码为 stdout 上的 UTF-8 JSONL。"""

    def __init__(self, output: BinaryIO | None = None) -> None:
        """使用注入的二进制流，默认直接写 ``sys.stdout.buffer``。"""

        self._output = sys.stdout.buffer if output is None else output
        self._failure: Exception | None = None

    async def emit(self, event: RunEvent) -> None:
        """序列化一个 Event，并确保整行已写入和刷新。"""

        if self._failure is not None:
            # 首次写失败后保持失败状态，禁止后续 Event 造成“部分成功”的假象。
            raise self._failure
        try:
            line = json.dumps(
                serialize_run_event(event),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload = f"{line}\n".encode()
            written = self._output.write(payload)
            if written is not None and written != len(payload):
                raise OSError(f"wrote {written} of {len(payload)} JSONL bytes")
            self._output.flush()
        except Exception as error:
            self._failure = error
            raise

    def raise_if_failed(self) -> None:
        """在尽力投递 Event 后恢复 Host 的 fail-closed 语义。"""

        if self._failure is not None:
            raise OSError(f"failed to write JSONL Events: {self._failure}") from self._failure


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """在没有子命令时启动交互式 Textual Host。"""

    # 根命令自身是适配器：裸 phi 只把 cwd 交给 TUI，不构建第二套运行逻辑。
    if ctx.invoked_subcommand is None:
        run_tui(cwd=Path.cwd().resolve())


def _run_async[T](operation: Coroutine[Any, Any, T]) -> T:
    """在同步 Typer callback 中运行异步服务，并统一映射异常。"""

    try:
        return asyncio.run(operation)
    except KeyboardInterrupt:
        render_cancelled("Operation cancelled")
        raise typer.Exit(130) from None
    except Exception as error:
        _exit_operational(error)


def _exit_operational(error: Exception) -> Never:
    """渲染一个脱敏操作失败，不向终端暴露 traceback。"""

    message = redact_text(str(error)) or type(error).__name__
    render_error(message)
    raise typer.Exit(1) from None


def _session_storage() -> SessionStorage:
    """按当前 Settings 创建 Session 持久化入口。"""

    return SessionStorage(_load_settings().session_dir)


def _load_settings() -> Settings:
    """加载环境设置，并把解析错误转换为 CLI 操作失败。"""

    try:
        return _settings_factory()
    except Exception as error:
        _exit_operational(error)


async def _discover_models(settings: Settings) -> list[ModelInfo]:
    """用当前网关配置发现可选择的 Model 目录。"""

    return await _model_discovery(model_config_from_settings(settings))


def _report_session_diagnostics(handles: list[SessionHandle]) -> None:
    """跨多个 Session handle 去重并脱敏诊断。"""

    reported: set[str] = set()
    for handle in handles:
        for diagnostic in handle.diagnostics:
            if diagnostic not in reported:
                reported.add(diagnostic)
                render_warning(redact_text(diagnostic))


def _report_diagnostics(diagnostics: tuple[str, ...]) -> None:
    """把已聚合的诊断逐条作为警告渲染。"""

    for diagnostic in diagnostics:
        render_warning(redact_text(diagnostic))


@session_app.command("list", help="List all validated Sessions, newest first.")
def session_list_command() -> None:
    """列出所有已验证 Session，并把最近更新的放在最前。"""

    # Session 服务负责验证磁盘数据；Host 只决定面向用户的排序和渲染。
    handles = _run_async(list_session_handles(_session_storage()))
    handles.sort(
        key=lambda handle: (handle.metadata.updated_at, handle.session_id),
        reverse=True,
    )
    _report_session_diagnostics(handles)
    if not handles:
        render_empty_sessions()
        return
    render_sessions(handles)


@session_app.command("resume", help="Resume one validated Session in the Textual Host.")
def session_resume_command(
    session_id: Annotated[str, typer.Argument(metavar="ID")],
) -> None:
    """在 Textual Host 中恢复一个已验证 Session。"""

    handle = _run_async(resume_session(_session_storage(), session_id))
    _report_session_diagnostics([handle])
    run_tui(initial_session=handle, cwd=Path.cwd())


@session_app.command(
    "fork",
    help="Create an exact Fork at an Entry in the selected Conversation View.",
)
def session_fork_command(
    session_id: Annotated[str, typer.Argument(metavar="ID")],
    entry_id: Annotated[str, typer.Argument(metavar="ENTRY_ID")],
    model: Annotated[str | None, typer.Option("--model")] = None,
) -> None:
    """在所选 Conversation View 的指定 Entry 创建精确 Fork。"""

    # 先恢复源 Session，确保 Fork 点由持久化 Entry 树验证而非信任命令行文本。
    settings = _load_settings()
    storage = SessionStorage(settings.session_dir)
    source = _run_async(resume_session(storage, session_id))
    _report_session_diagnostics([source])
    selected_model: str | None = None
    # 显式 Model 必须先经过目录校验；未指定时沿用 Fork 服务的分支规则。
    if model is not None:
        try:
            selected_model = require_explicit_model_id(model)
        except Exception as error:
            _exit_operational(error)
        models = _run_async(_discover_models(settings))
        try:
            require_available_model(selected_model, models)
        except Exception as error:
            _exit_operational(error)
    forked = _run_async(
        fork_session(
            storage,
            source,
            entry_id,
            model=selected_model,
        )
    )
    render_session_fork(forked.session_id, source.session_id, entry_id)


@app.command("context", help="Inspect the complete Context Phi would send to the Model.")
def context_command(
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """检查 Phi 将发送给 Model 的完整 Context。"""

    # Session 选择与 Context 冻结由共享管理服务执行；此 callback 只选择输出格式。
    settings = _load_settings()
    selected = _run_async(select_context_session(SessionStorage(settings.session_dir), session_id))
    outcome = _run_async(
        execute_context_inspection(
            cwd=Path.cwd(),
            runtime_factory=_runtime_factory,
            selected_session=selected,
        )
    )
    _report_diagnostics(outcome.diagnostics)
    if json_output:
        typer.echo(
            json.dumps(
                outcome.to_document(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    render_context(outcome)


def _selected_mcp_source(
    global_scope: bool,
) -> tuple[Literal["project", "global"], Path]:
    """把 ``--global`` 映射为单一配置来源及其精确路径。"""

    if global_scope:
        return "global", _global_mcp_path()
    return "project", _project_mcp_path(Path.cwd())


@mcp_app.command(
    "add",
    help="Add one stdio MCP server; tokens after -- are literal command data.",
)
def mcp_add_command(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    command: Annotated[list[str], typer.Argument(metavar="COMMAND [ARGS...]")],
    global_scope: Annotated[bool, typer.Option("--global")] = False,
) -> None:
    """向选定来源添加一个 stdio MCP server。"""

    # Typer 已把 -- 后内容保留为列表；Host 不再经 shell 重新解析这些参数。
    source, path = _selected_mcp_source(global_scope)
    _run_async(
        add_mcp_server(
            path,
            name,
            command[0],
            tuple(command[1:]),
            source=source,
        )
    )
    render_confirmation(f"Added {source} MCP server {name!r}.")


@mcp_app.command("list", help="List effective merged or global-only MCP configuration.")
def mcp_list_command(
    global_scope: Annotated[bool, typer.Option("--global")] = False,
) -> None:
    """列出合并后的有效 MCP 配置，或仅列出用户级配置。"""

    # 非 global 模式同时交给服务两个来源，由服务应用 project-overrides-global 规则。
    servers = _run_async(
        list_configured_mcp_servers(
            _global_mcp_path(),
            _project_mcp_path(Path.cwd()),
            global_only=global_scope,
        )
    )
    render_mcp_servers(servers, global_only=global_scope)


@mcp_app.command("remove", help="Remove one MCP server from exactly one selected source.")
def mcp_remove_command(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    global_scope: Annotated[bool, typer.Option("--global")] = False,
) -> None:
    """只从明确选择的一个来源删除 MCP server。"""

    source, path = _selected_mcp_source(global_scope)
    _run_async(remove_mcp_server(path, name, source=source))
    render_confirmation(f"Removed {source} MCP server {name!r}.")


@app.command("doctor", help="Check whether this workspace can start a Run with the default Model.")
def doctor_command(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show successful-check details and timings."),
    ] = False,
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="Start enabled MCP servers and send one streaming Model request.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one versioned machine-readable report."),
    ] = False,
) -> None:
    """检查当前工作区能否使用默认 Model 启动 Run。"""

    # 依赖对象集中注入真实或测试探针，使 callback 仍保持薄 Host。
    dependencies = DoctorDependencies(
        settings_factory=_settings_factory,
        model_discovery=_model_discovery,
        model_probe=_model_probe,
        mcp_probe=_mcp_probe,
    )
    # JSON 模式禁止 spinner 污染机器可读 stdout；普通模式可展示终端进度。
    with doctor_progress(enabled=not json_output):
        report = _run_async(
            run_doctor(
                Path.cwd(),
                deep=deep,
                dependencies=dependencies,
            )
        )
    if json_output:
        typer.echo(
            json.dumps(
                report.to_document(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        render_doctor(report, verbose=verbose)
    if not report.healthy:
        raise typer.Exit(1)


@app.command("run", help="Run one persistent headless Agent task.")
def run_command(
    task: Annotated[str, typer.Argument(help="One task for the Agent to handle.")],
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    max_steps: Annotated[int, typer.Option("--max-steps", min=1)] = 20,
    model: Annotated[str | None, typer.Option("--model")] = None,
) -> None:
    """执行一个持久化的无界面 Agent 任务。"""

    if not task.strip():
        raise typer.BadParameter("TASK must contain non-whitespace text", param_hint="TASK")
    events: _JsonlEventWriter | None = None
    try:
        # JSON 模式把共享 Event bus 接到 JSONL writer；普通模式只呈现最终输出。
        if json_output:
            events = _JsonlEventWriter()
        outcome = asyncio.run(
            execute_headless_run(
                task,
                cwd=Path.cwd(),
                runtime_factory=_runtime_factory,
                session_id=session_id,
                selected_model=model,
                max_steps=max_steps,
                events=events,
                report_session=lambda value: typer.echo(f"session_id={value}", err=True),
                report_diagnostic=lambda value: render_warning(redact_text(value)),
            )
        )
        if events is not None:
            events.raise_if_failed()
    except KeyboardInterrupt:
        render_cancelled("Run cancelled")
        raise typer.Exit(130) from None
    except Exception as error:
        _exit_operational(error)

    # CLI 在最外层把 Harness Run 终态稳定映射为文档约定的退出码。
    if outcome.result.status is RunStatus.COMPLETED:
        if not json_output:
            typer.echo(outcome.result.output)
        return
    if outcome.result.status is RunStatus.FAILED:
        assert outcome.result.error is not None
        render_failure(f"Run failed: {redact_text(str(outcome.result.error))}")
        raise typer.Exit(1)
    if outcome.result.status is RunStatus.MAX_STEPS:
        render_exhausted(f"Run exhausted its Step budget ({max_steps})")
        raise typer.Exit(2)
    render_cancelled("Run cancelled")
    raise typer.Exit(130)
