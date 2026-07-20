"""CLI 的 Session 选择与只读 Context 检查编排。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phi.bootstrap import HostRuntime
from phi.cli.model_selection import resolve_available_model
from phi.sessions import (
    ContextInspection,
    SessionHandle,
    SessionStorage,
    inspect_context,
    list_session_handles,
    resume_session,
)


class ContextSelectionError(ValueError):
    """表示无法解析 Context 检查目标或有效 Model。"""


type RuntimeFactory = Callable[[Path], Awaitable[HostRuntime]]


@dataclass(frozen=True)
class ContextCommandOutcome:
    """保存 Context 命令渲染和 JSON 输出所需的不可变快照。"""

    handle: SessionHandle
    model_id: str
    inspection: ContextInspection
    diagnostics: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        """把检查结果投影为稳定、可机器读取的版本化文档。"""

        # 显式列出公开字段，避免把内部 dataclass/Pydantic 序列化细节变成 CLI 契约。
        context = self.inspection.context
        request = self.inspection.request
        metadata = self.handle.metadata
        return {
            "schema_version": 1,
            "session": {
                "id": metadata.id,
                "name": metadata.name,
                "leaf_id": metadata.leaf_id,
                "origin": metadata.origin,
                "parent_session_id": metadata.parent_session_id,
                "fork_point_entry_id": metadata.fork_point_entry_id,
            },
            "model": self.model_id,
            "context": {
                "system_prompt": context.system_prompt,
                "tools": list(context.tools),
                "messages": list(context.messages),
                "dropped_summary": context.dropped_summary,
            },
            "model_request": {
                "messages": request.messages,
                "tools": request.tools,
                "model": request.model,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            },
            "character_counts": dict(self.inspection.character_counts),
            "token_estimate": {
                "tokens": self.inspection.estimate.tokens,
                "local_tokens": self.inspection.estimate.local_tokens,
                "used_provider_anchor": self.inspection.estimate.used_provider_anchor,
            },
            "input_limits": {
                "effective": self.inspection.effective_input_limit,
                "safe": self.inspection.safe_prompt_limit,
            },
            "diagnostics": list(self.diagnostics),
        }


async def select_context_session(
    storage: SessionStorage,
    session_id: str | None,
) -> SessionHandle:
    """选择显式 Session，或确定性地选择最近更新的 Session。"""

    if session_id is not None:
        return await resume_session(storage, session_id)
    handles = await list_session_handles(storage)
    if not handles:
        raise ContextSelectionError("No Sessions found; pass --session after creating one")
    # Session ID 作为更新时间相同情况下的稳定次级排序键。
    return max(handles, key=lambda handle: (handle.metadata.updated_at, handle.session_id))


async def execute_context_inspection(
    *,
    cwd: Path,
    runtime_factory: RuntimeFactory,
    selected_session: SessionHandle,
) -> ContextCommandOutcome:
    """构建 Run 将使用的同一 cwd 级 Context，但不调用 Model。"""

    runtime = await runtime_factory(cwd)
    if not isinstance(runtime, HostRuntime):
        raise TypeError("runtime factory must return HostRuntime")
    try:
        # 在 runtime 自己的 storage 上重新恢复，确保检查的是该 cwd 运行时看到的最新 handle。
        handle = await resume_session(runtime.storage, selected_session.session_id)
        model_id, model_info = resolve_available_model(
            handle.metadata.model,
            runtime.settings.default_model,
            available_models=runtime.available_models,
            missing_message=(
                "no Model was selected; configure PHI_DEFAULT_MODEL or select a branch Model"
            ),
        )
        # 共享检查服务冻结 Conversation View、Context 和规范化 ModelRequest。
        inspection = await inspect_context(
            runtime.storage,
            handle,
            settings=runtime.settings,
            model_info=model_info,
            tools=runtime.resources.tools,
            instructions=runtime.resources.instruction_assembly,
        )
        # 按发现顺序合并各层诊断，同时保持一次输出只出现一次。
        diagnostics = tuple(
            dict.fromkeys(
                str(item)
                for item in (
                    *runtime.resources.diagnostics,
                    *selected_session.diagnostics,
                    *handle.diagnostics,
                    *inspection.diagnostics,
                )
            )
        )
        return ContextCommandOutcome(handle, model_id, inspection, diagnostics)
    finally:
        # Context 命令虽不调用 Model，仍可能构建需显式关闭的 cwd 级资源。
        await runtime.close()
