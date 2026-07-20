"""组合 Session 持久化、Conversation View、Context 构造与 Harness Run。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from phi.harness import (
    AtomicConversationUnit,
    ContextCapacityError,
    EventBus,
    EventEmitter,
    EventListener,
    Hooks,
    InvalidCompactionSummaryError,
    ModelCallCompleted,
    ModelCallStarted,
    NothingToCompactError,
    PromptBudgetAnchor,
    RunEvent,
    RunFinished,
    RunResult,
    RunStatus,
    Step,
    ToolCallCompleted,
    effective_input_limit,
    run,
    safe_prompt_limit,
    select_compaction_units,
    should_compact,
)
from phi.harness.compaction import estimate_prompt_tokens, estimate_request_tokens
from phi.harness.context import Context, build_context
from phi.instructions import InstructionAssembly
from phi.model import (
    Model,
    ModelContextLimitError,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ToolResult,
    serialize_assistant_response,
    serialize_tool_result,
)
from phi.sessions.entries import (
    AssistantMessageEntry,
    CompactionEntry,
    Entry,
    ToolResultEntry,
    UserMessageEntry,
)
from phi.sessions.errors import (
    CorruptSessionError,
    InvalidSessionLeafError,
    MissingEntryParentError,
    SessionLineageCycleError,
)
from phi.sessions.inspection import (
    ContextInspection,
    InspectedMessage,
    InspectedSummary,
    InspectedTool,
    ProjectionCounts,
)
from phi.sessions.metadata import SessionMetadata
from phi.sessions.storage import LoadedSession, SessionStorage
from phi.sessions.trace import TraceWriter
from phi.settings import Settings
from phi.tools import ToolDispatcher, ToolRegistry


@dataclass(frozen=True)
class SessionHandle:
    """Host 持有的不可变 Session 游标及仅限运行时的预算信息。"""

    session_id: str
    leaf_id: str | None
    session_file: Path
    metadata: SessionMetadata
    revision: int
    prompt_budget_anchor: PromptBudgetAnchor | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationView:
    """沿 Session Entry 树一条路径物化出的对话与有效设置。"""

    session_id: str
    leaf_id: str | None
    entries: tuple[Entry, ...]
    model: str | None
    dropped_summary: str | None = None


@dataclass(frozen=True)
class SessionPresentation:
    """供 Host 展示与导航使用的只读完整选定路径。"""

    session_id: str
    leaf_id: str | None
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class RunInvocation:
    """绑定到一次确切 Harness Run、供生命周期扩展使用的依赖集合。"""

    run_id: str
    session_id: str
    selected_model: str | None
    storage: SessionStorage
    settings: Settings
    model: Model
    model_info: ModelInfo | None
    tools: ToolRegistry
    dispatcher: ToolDispatcher
    stable_instructions: str
    max_steps: int
    hooks: Hooks | None


class RunLifecycle(Protocol):
    """在服务边界拦截一次确切 Run 生命周期的通用协议。"""

    async def before_run(
        self,
        invocation: RunInvocation,
        context: object | None,
    ) -> Mapping[str, object]:
        """进入 Run 前返回注入 ToolDispatcher 的可信运行时值。"""

        ...

    async def after_run(
        self,
        invocation: RunInvocation,
        context: object | None,
    ) -> None:
        """Run 结束后清理属于该 Run 的资源。"""

        ...


@dataclass
class _ObservedStep:
    """从 Events 逐步拼装、尚未确认可持久化的 Step。"""

    index: int
    request: ModelRequest | None = None
    response: ModelResponse | None = None
    tool_results: list[ToolResult] | None = None


@dataclass
class _ActiveSend:
    """在可取消发送期间跟踪最新 SessionHandle 与当前 Run ID。"""

    handle: SessionHandle
    run_id: str | None = None


class _RunEventBoundary:
    """在 Session 所属生命周期清理完成前暂存终止通知。"""

    def __init__(self, event_bus: EventBus[RunEvent]) -> None:
        """包装底层 EventBus，并初始化每个 Run 的终止状态。"""

        self._event_bus = event_bus
        self._terminal_events: dict[str, RunFinished] = {}
        self._next_indexes: dict[str, int] = {}

    async def emit(self, event: RunEvent) -> None:
        """立即转发普通 Event，但暂存 ``RunFinished``。"""

        self._next_indexes[event.run_id] = event.event_index + 1
        if isinstance(event, RunFinished):
            self._terminal_events[event.run_id] = event
            return
        await self._event_bus.emit(event)

    async def finish(self, run_id: str) -> None:
        """生命周期清理成功后发布原始终止 Event。"""

        event = self._terminal_events.pop(run_id, None)
        if event is not None:
            await self._event_bus.emit(event)
        self._next_indexes.pop(run_id, None)

    async def cancel(self, run_id: str, result: RunResult) -> None:
        """取消时以正确的后续索引发布合成终止 Event。"""

        pending = self._terminal_events.pop(run_id, None)
        next_index = self._next_indexes.pop(run_id, None)
        if pending is None and next_index is None:
            return
        event_index = pending.event_index if pending is not None else next_index
        assert event_index is not None
        await self._event_bus.emit(RunFinished(run_id, event_index, result))


class _SafeStepRecorder:
    """从 Event 流恢复取消时仍能安全持久化的完整 Steps。"""

    def __init__(self) -> None:
        """初始化按 Run 与 Step 索引组织的观察状态。"""

        self._run_order: list[str] = []
        self._steps: dict[tuple[str, int], _ObservedStep] = {}

    def __call__(self, event: RunEvent) -> None:
        """消费 Model/Tool 边界 Events，逐步补全 Step。"""

        if event.run_id not in self._run_order:
            self._run_order.append(event.run_id)
        if isinstance(event, ModelCallStarted):
            self._steps[(event.run_id, event.step_index)] = _ObservedStep(
                index=event.step_index,
                request=event.request,
                tool_results=[],
            )
        elif isinstance(event, ModelCallCompleted):
            observed = self._steps[(event.run_id, event.step_index)]
            observed.response = event.response
        elif isinstance(event, ToolCallCompleted):
            observed = self._steps[(event.run_id, event.step_index)]
            assert observed.tool_results is not None
            observed.tool_results.append(event.result)

    def safe_steps(self) -> tuple[Step, ...]:
        """返回按 Run 顺序排列、Tool Call/Result 完整匹配的连续 Steps。"""

        safe: list[Step] = []
        for run_id in self._run_order:
            observed_steps = sorted(
                (
                    observed
                    for (observed_run_id, _), observed in self._steps.items()
                    if observed_run_id == run_id
                ),
                key=lambda item: item.index,
            )
            for observed in observed_steps:
                # 一个 Run 遇到首个不完整 Step 就停止，不能越过缺口拼接后续状态。
                if observed.request is None or observed.response is None:
                    break
                results = tuple(observed.tool_results or ())
                calls = observed.response.tool_calls
                if calls and (
                    len(calls) != len(results)
                    or [call.id for call in calls] != [result.call_id for result in results]
                ):
                    # Assistant Tool Calls 与 Tool Results 必须按 call_id 一一对应。
                    break
                safe.append(
                    Step(
                        index=observed.index,
                        request=observed.request,
                        response=observed.response,
                        tool_results=results,
                    )
                )
        return tuple(safe)


async def create_session(
    storage: SessionStorage,
    *,
    model: str | None = None,
    name: str | None = None,
    origin: Literal["new", "subagent"] = "new",
    parent_session_id: str | None = None,
) -> SessionHandle:
    """创建普通或隔离 Subagent Session，并返回初始不可变 Handle。

    Subagent Session 只记录父 Session 的 Delegation 谱系，不继承父 Entries。
    """

    if origin == "subagent":
        if parent_session_id is None:
            raise ValueError("Subagent Sessions require a parent Session ID")
        await storage.load(parent_session_id)
    elif parent_session_id is not None:
        raise ValueError("new Sessions cannot contain parent lineage")
    envelope = await storage.create(
        model=model,
        name=name,
        origin=origin,
        parent_session_id=parent_session_id,
    )
    return _handle(storage, envelope.metadata, envelope.revision)


async def resume_session(storage: SessionStorage, session_id: str) -> SessionHandle:
    """加载、校验并物化指定 Session 的当前分支。"""

    state = await storage.load_state(session_id)
    await _session_branch_points(storage, state)
    handle = _handle(
        storage,
        state.envelope.metadata,
        state.envelope.revision,
        diagnostics=state.diagnostics,
    )
    await materialize_conversation(storage, handle)
    return handle


async def list_sessions(storage: SessionStorage) -> list[SessionMetadata]:
    """列出经过完整恢复校验的 Session 元数据。"""

    return [handle.metadata for handle in await list_session_handles(storage)]


async def list_session_handles(storage: SessionStorage) -> list[SessionHandle]:
    """加载并校验每个 Session，且不隐藏恢复诊断。"""

    sessions: list[SessionHandle] = []
    for envelope in await storage.list_metadata():
        sessions.append(await resume_session(storage, envelope.metadata.id))
    return sessions


async def list_leaves(
    storage: SessionStorage,
    handle: SessionHandle,
) -> tuple[str, ...]:
    """列出当前 Session Entry 树中所有本地 leaf。"""

    state = await storage.load_state(handle.session_id)
    await _session_branch_points(storage, state)
    if not state.entries:
        fork_point = state.envelope.metadata.fork_point_entry_id
        return (fork_point,) if fork_point is not None else ()
    referenced = {entry.parent_id for entry in state.entries if entry.parent_id is not None}
    # 未被任何节点作为 parent_id 引用的 Entry 就是树叶。
    return tuple(entry.id for entry in state.entries if entry.id not in referenced)


async def switch_leaf(
    storage: SessionStorage,
    handle: SessionHandle,
    entry_id: str,
) -> SessionHandle:
    """把 Session 当前 leaf 切换到一个合法消息边界。"""

    state = await storage.load_state(handle.session_id)
    available = {entry.id for entry in state.entries}
    if state.envelope.metadata.fork_point_entry_id is not None:
        available.add(state.envelope.metadata.fork_point_entry_id)
    if entry_id not in available or entry_id not in await _session_branch_points(storage, state):
        raise InvalidSessionLeafError(handle.session_id, entry_id)
    metadata = handle.metadata.model_copy(
        update={"leaf_id": entry_id, "updated_at": datetime.now(UTC)}
    )
    envelope = await storage.replace_metadata(
        handle.session_id,
        expected_revision=handle.revision,
        metadata=metadata,
    )
    return _handle(storage, envelope.metadata, envelope.revision)


async def fork_session(
    storage: SessionStorage,
    handle: SessionHandle,
    entry_id: str,
    *,
    model: str | None = None,
    name: str | None = None,
) -> SessionHandle:
    """在选定 Entry 创建引用父历史而不复制前缀的新 Fork Session。"""

    state = await storage.load_state(handle.session_id)
    selected_path = await _materialize_path(
        storage,
        state,
        handle.leaf_id,
        seen_sessions=set(),
    )
    if entry_id not in _validate_path(selected_path, handle.session_id):
        raise InvalidSessionLeafError(handle.session_id, entry_id)
    envelope = await storage.create(
        model=model if model is not None else handle.metadata.model,
        name=name,
        parent_session_id=handle.session_id,
        fork_point_entry_id=entry_id,
        origin="fork",
    )
    return _handle(storage, envelope.metadata, envelope.revision)


async def select_model(
    storage: SessionStorage,
    handle: SessionHandle,
    model: str,
) -> SessionHandle:
    """为 Session 选择 Model；已有本地 Model 输出时通过 Fork 保留历史语义。"""

    model = model.strip()
    if not model:
        raise ValueError("selected Model ID must be non-empty")
    if model == handle.metadata.model:
        return handle
    state = await storage.load_state(handle.session_id)
    selected_path = await _materialize_path(
        storage,
        state,
        handle.leaf_id,
        seen_sessions=set(),
    )
    _validate_path(selected_path, handle.session_id)
    local_entry_ids = {entry.id for entry in state.entries}
    # 一旦当前 Session 已产生本地 Assistant 输出，原地改 Model 会混淆分支语义。
    if any(
        isinstance(entry, AssistantMessageEntry) and entry.id in local_entry_ids
        for entry in selected_path
    ):
        if handle.leaf_id is None:
            raise AssertionError("a branch with Model output must have a current leaf")
        return await fork_session(
            storage,
            handle,
            handle.leaf_id,
            model=model,
        )
    metadata = handle.metadata.model_copy(update={"model": model, "updated_at": datetime.now(UTC)})
    envelope = await storage.replace_metadata(
        handle.session_id,
        expected_revision=handle.revision,
        metadata=metadata,
    )
    return _handle(storage, envelope.metadata, envelope.revision)


async def rename_session(
    storage: SessionStorage,
    handle: SessionHandle,
    name: str | None,
) -> SessionHandle:
    """更新 Session 展示名称，同时保留仍有效的运行时预算锚点。"""

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Session name must be non-empty when supplied")
    metadata = handle.metadata.model_copy(update={"name": name, "updated_at": datetime.now(UTC)})
    envelope = await storage.replace_metadata(
        handle.session_id,
        expected_revision=handle.revision,
        metadata=metadata,
    )
    return _with_runtime(
        _handle(storage, envelope.metadata, envelope.revision),
        prompt_budget_anchor=handle.prompt_budget_anchor,
    )


async def materialize_conversation(
    storage: SessionStorage,
    handle: SessionHandle,
) -> ConversationView:
    """从当前 leaf 物化有限 Context 之前的 Conversation View。"""

    presentation = await materialize_presentation(storage, handle)
    return _conversation_view_from_path(handle, presentation.entries)


def _conversation_view_from_path(
    handle: SessionHandle,
    path: tuple[Entry, ...],
) -> ConversationView:
    """将完整 Entry 路径投影成应用最近 Compaction 后的 Conversation View。"""

    dropped_summary: str | None = None
    visible: tuple[Entry, ...] = path
    compaction_indexes = [
        index for index, entry in enumerate(path) if isinstance(entry, CompactionEntry)
    ]
    if compaction_indexes:
        # 最新 Compaction 覆盖更早摘要；未来 Context 从其摘要和保留后缀开始。
        compaction_index = compaction_indexes[-1]
        compaction = path[compaction_index]
        assert isinstance(compaction, CompactionEntry)
        first_kept = next(
            (
                index
                for index, entry in enumerate(path)
                if entry.id == compaction.first_kept_entry_id
            ),
            None,
        )
        if first_kept is None or first_kept >= compaction_index:
            raise CorruptSessionError(
                handle.session_id,
                "Compaction Entry references an invalid retained Entry",
            )
        dropped_summary = compaction.summary
        visible = tuple(
            entry
            for index, entry in enumerate(path)
            if index >= first_kept and not isinstance(entry, CompactionEntry)
        )
    return ConversationView(
        session_id=handle.session_id,
        leaf_id=handle.leaf_id,
        entries=visible,
        model=handle.metadata.model,
        dropped_summary=dropped_summary,
    )


async def materialize_presentation(
    storage: SessionStorage,
    handle: SessionHandle,
) -> SessionPresentation:
    """物化未经过 Context 过滤的完整持久化选定路径。"""

    state = await storage.load_state(handle.session_id)
    path = await _materialize_path(
        storage,
        state,
        handle.leaf_id,
        seen_sessions=set(),
    )
    _validate_path(path, handle.session_id)
    return SessionPresentation(handle.session_id, handle.leaf_id, path)


async def inspect_context(
    storage: SessionStorage,
    handle: SessionHandle,
    *,
    settings: Settings,
    model_info: ModelInfo | None,
    tools: ToolRegistry,
    instructions: InstructionAssembly,
) -> ContextInspection:
    """只读检查 Session 到 ModelRequest 的精确投影与预算。

    此操作不调用 Model、不追加 Entry、不切换 leaf，也不触发 Compaction。
    """

    presentation = await materialize_presentation(storage, handle)
    view = _conversation_view_from_path(handle, presentation.entries)
    context = _context_for_view(view, tools, instructions.stable_instructions)
    selected_model = view.model or settings.default_model or None
    request = context.to_request(model=selected_model)
    estimate = estimate_prompt_tokens(
        request,
        model_id=selected_model or "<unresolved>",
        anchor=handle.prompt_budget_anchor,
    )
    effective_limit = effective_input_limit(model_info, settings.compaction)
    diagnostics: tuple[str, ...] = ()
    safe_limit: int | None = None
    if effective_limit is None:
        # 未知窗口时只报告 best-effort 估算，不能伪造安全上限。
        diagnostics = ("Model input limit is unknown; proactive Context budgeting is best-effort",)
    else:
        safe_limit = safe_prompt_limit(effective_limit, settings.compaction)
    inspected_tools = tuple(_inspect_tool(schema) for schema in context.tools)
    inspected_messages = tuple(
        _inspect_message(index, message) for index, message in enumerate(context.messages, start=1)
    )
    inspected_summary = (
        InspectedSummary(context.dropped_summary, len(context.dropped_summary))
        if context.dropped_summary is not None
        else None
    )
    return ContextInspection(
        context=context,
        request=request,
        model_id=selected_model,
        projection=ProjectionCounts(
            session_path_entries=len(presentation.entries),
            conversation_view_entries=len(view.entries),
            context_messages=len(context.messages),
            request_messages=len(request.messages),
        ),
        instructions=instructions.sections,
        tools=inspected_tools,
        messages=inspected_messages,
        dropped_summary=inspected_summary,
        estimate=estimate,
        provider_anchor_prompt_tokens=(
            handle.prompt_budget_anchor.prompt_tokens
            if estimate.used_provider_anchor and handle.prompt_budget_anchor is not None
            else None
        ),
        effective_input_limit=effective_limit,
        safe_prompt_limit=safe_limit,
        diagnostics=diagnostics,
    )


def _inspect_tool(schema: Mapping[str, Any]) -> InspectedTool:
    """为一个 OpenAI-compatible Tool schema 添加展示元数据。"""

    function = schema.get("function")
    function = function if isinstance(function, Mapping) else {}
    name = function.get("name")
    description = function.get("description")
    return InspectedTool(
        name=name if isinstance(name, str) else "Unnamed Tool",
        description=description if isinstance(description, str) else "",
        schema=schema,
        characters=_json_characters(schema),
    )


def _inspect_message(index: int, message: Mapping[str, Any]) -> InspectedMessage:
    """按消息语义生成便于 Host 阅读的标签与内容。"""

    role = message.get("role")
    if role == "tool":
        label = f"Tool Result {index}"
    elif role == "assistant" and message.get("tool_calls"):
        label = f"Assistant Tool Calls {index}"
    elif role == "assistant":
        label = f"Assistant message {index}"
    elif role == "user":
        label = f"User message {index}"
    else:
        label = f"{str(role or 'Unknown').title()} message {index}"
    return InspectedMessage(
        index=index,
        label=label,
        readable_content=_readable_message_content(message),
        message=message,
        characters=_json_characters(message),
    )


def _readable_message_content(message: Mapping[str, Any]) -> str:
    """把消息文本及 Tool Calls 展开为人类可读内容。"""

    content = message.get("content")
    parts = [content] if isinstance(content, str) and content else []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            parts.append(f"Tool Call: {name or 'unnamed'}\nArguments: {arguments or '{}'}")
    if parts:
        return "\n\n".join(parts)
    return "(no text content)"


def _json_characters(value: Mapping[str, Any]) -> int:
    """按预算使用的紧凑 UTF-8 JSON 形状统计字符数。"""

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


async def manual_compact(
    handle: SessionHandle,
    *,
    storage: SessionStorage,
    settings: Settings,
    model: Model,
    model_info: ModelInfo | None,
    tools: ToolRegistry,
    stable_instructions: str,
    focus: str | None = None,
) -> SessionHandle:
    """按可选关注点手动 Compaction 当前 Conversation View。"""

    return await _compact(
        handle,
        storage=storage,
        settings=settings,
        model=model,
        model_info=model_info,
        tools=tools,
        stable_instructions=stable_instructions,
        focus=focus,
        pending_entry_id=None,
    )


async def send_message(
    handle: SessionHandle,
    text: str,
    *,
    storage: SessionStorage,
    settings: Settings,
    model: Model,
    model_info: ModelInfo | None,
    tools: ToolRegistry,
    dispatcher: ToolDispatcher,
    stable_instructions: str,
    max_steps: int,
    hooks: Hooks | None = None,
    events: EventEmitter[RunEvent] | None = None,
    lifecycle: RunLifecycle | None = None,
    lifecycle_context: object | None = None,
) -> tuple[SessionHandle, RunResult]:
    """持久化用户消息，构建 Context，执行一次 Run 并提交完整 Steps。

    该服务是 Session 与 Harness 的组合边界：负责阈值 Compaction、一次有界溢出
    重试、Trace 记录以及取消时安全 Step 的持久化。
    """

    if not text.strip():
        raise ValueError("User messages must not be empty")
    if handle.metadata.model is None and settings.default_model:
        handle = await select_model(storage, handle, settings.default_model)
    await materialize_conversation(storage, handle)
    selected_model = handle.metadata.model or settings.default_model or None
    user_entry = UserMessageEntry(parent_id=handle.leaf_id, content=text)
    # 用户消息在 Model 调用前先持久化，保证失败或取消后仍能恢复请求分支。
    handle = await _append(
        storage,
        handle,
        (user_entry,),
        prompt_budget_anchor=handle.prompt_budget_anchor,
    )
    run_events, step_recorder, trace_writer = _run_event_bus(storage, handle, events)
    active = _ActiveSend(handle)
    try:
        return await _continue_send(
            active,
            user_entry=user_entry,
            selected_model=selected_model,
            storage=storage,
            settings=settings,
            model=model,
            model_info=model_info,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions=stable_instructions,
            max_steps=max_steps,
            hooks=hooks,
            run_events=run_events,
            lifecycle=lifecycle,
            lifecycle_context=lifecycle_context,
        )
    except asyncio.CancelledError:
        # Harness 已完成的原子 Steps 仍是有效对话历史；从 Event 记录器中安全恢复。
        cancelled_handle, result = await _cancelled_result(
            storage,
            active.handle,
            step_recorder,
            trace_writer,
        )
        if active.run_id is not None:
            await run_events.cancel(active.run_id, result)
        return cancelled_handle, result


async def _continue_send(
    active: _ActiveSend,
    *,
    user_entry: UserMessageEntry,
    selected_model: str | None,
    storage: SessionStorage,
    settings: Settings,
    model: Model,
    model_info: ModelInfo | None,
    tools: ToolRegistry,
    dispatcher: ToolDispatcher,
    stable_instructions: str,
    max_steps: int,
    hooks: Hooks | None,
    run_events: _RunEventBoundary,
    lifecycle: RunLifecycle | None,
    lifecycle_context: object | None,
) -> tuple[SessionHandle, RunResult]:
    """完成阈值预算、Harness 执行、溢出恢复与结果持久化。"""

    handle = active.handle
    inspection_instructions = InstructionAssembly.from_prompt(stable_instructions)
    inspection = await inspect_context(
        storage,
        handle,
        settings=settings,
        model_info=model_info,
        tools=tools,
        instructions=inspection_instructions,
    )
    request = inspection.context.to_request(model=selected_model)
    compacted = False
    policy = settings.compaction
    effective_limit = effective_input_limit(model_info, policy)
    if effective_limit is None:
        # 未知输入窗口时不能声称主动预算安全，只保留诊断并让 provider 决定。
        handle = _with_runtime(
            handle,
            prompt_budget_anchor=handle.prompt_budget_anchor,
            diagnostics=(
                *handle.diagnostics,
                "Model input limit is unknown; proactive Context compaction was skipped",
            ),
        )
        active.handle = handle
    else:
        safe_limit = safe_prompt_limit(effective_limit, policy)
        estimate = estimate_prompt_tokens(
            request,
            model_id=selected_model or "<unresolved>",
            anchor=handle.prompt_budget_anchor,
        )
        if should_compact(estimate.tokens, safe_limit, policy):
            # 每次 send_message 最多进行一次阈值 Compaction，避免不可收敛循环。
            try:
                handle = await _compact(
                    handle,
                    storage=storage,
                    settings=settings,
                    model=model,
                    model_info=model_info,
                    tools=tools,
                    stable_instructions=stable_instructions,
                    focus=None,
                    pending_entry_id=user_entry.id,
                )
                active.handle = handle
            except NothingToCompactError as error:
                raise ContextCapacityError(
                    "Context exceeds the safe prompt limit and nothing can be compacted"
                ) from error
            compacted = True
            inspection = await inspect_context(
                storage,
                handle,
                settings=settings,
                model_info=model_info,
                tools=tools,
                instructions=inspection_instructions,
            )
            request = inspection.context.to_request(model=selected_model)
            rebuilt = estimate_prompt_tokens(
                request,
                model_id=selected_model or "<unresolved>",
            )
            if rebuilt.tokens > safe_limit:
                # 稳定指令、Tools 或强制保留后缀过大时，继续摘要也无法解决容量问题。
                raise ContextCapacityError(
                    "Context remains over the safe prompt limit after one compaction"
                )
    invocation = RunInvocation(
        run_id=str(uuid4()),
        session_id=handle.session_id,
        selected_model=selected_model,
        storage=storage,
        settings=settings,
        model=model,
        model_info=model_info,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions=stable_instructions,
        max_steps=max_steps,
        hooks=hooks,
    )
    active.run_id = invocation.run_id
    result = await _execute_run(
        request,
        invocation=invocation,
        run_events=run_events,
        lifecycle=lifecycle,
        lifecycle_context=lifecycle_context,
    )

    if (
        not compacted
        and settings.compaction.enabled
        and result.status is RunStatus.FAILED
        and isinstance(result.error, ModelContextLimitError)
        and not any(step.tool_results for step in result.steps)
    ):
        # 仅在尚未 Compaction 且没有 Tool 副作用时，允许一次 provider 溢出恢复。
        try:
            handle = await _compact(
                handle,
                storage=storage,
                settings=settings,
                model=model,
                model_info=model_info,
                tools=tools,
                stable_instructions=stable_instructions,
                focus=None,
                pending_entry_id=user_entry.id,
            )
            active.handle = handle
        except NothingToCompactError as error:
            raise ContextCapacityError(
                "Context overflow cannot recover because nothing can be compacted"
            ) from error
        retry_inspection = await inspect_context(
            storage,
            handle,
            settings=settings,
            model_info=model_info,
            tools=tools,
            instructions=inspection_instructions,
        )
        retry_request = retry_inspection.context.to_request(model=selected_model)
        retry_invocation = replace(invocation, run_id=str(uuid4()))
        # 重试是新的 Run，拥有独立 Events 与生命周期所有权。
        active.run_id = retry_invocation.run_id
        result = await _execute_run(
            retry_request,
            invocation=retry_invocation,
            run_events=run_events,
            lifecycle=lifecycle,
            lifecycle_context=lifecycle_context,
        )

    completed_entries = _entries_from_steps(handle.leaf_id, result)
    # 只把完整 Step 拆成 Entries；失败 Run 的不完整尾部不会污染 Session 树。
    if completed_entries:
        handle = await _append(storage, handle, completed_entries)
        active.handle = handle

    anchor = _anchor_from_result(selected_model, result)
    if anchor is not None:
        handle = SessionHandle(
            session_id=handle.session_id,
            leaf_id=handle.leaf_id,
            session_file=handle.session_file,
            metadata=handle.metadata,
            revision=handle.revision,
            prompt_budget_anchor=anchor,
            diagnostics=handle.diagnostics,
        )
        active.handle = handle
    return handle, result


async def _execute_run(
    request: ModelRequest,
    *,
    invocation: RunInvocation,
    run_events: _RunEventBoundary,
    lifecycle: RunLifecycle | None,
    lifecycle_context: object | None,
) -> RunResult:
    """在生命周期扩展包围下执行 Harness Run，并延后终止 Event。"""

    trusted_tool_values: Mapping[str, object] = {}
    entered = False
    if lifecycle is not None:
        # 生命周期可注入 DelegationContext 等可信值，而不会暴露给 Model 参数。
        trusted_tool_values = await lifecycle.before_run(invocation, lifecycle_context)
        entered = True
    try:
        active_dispatcher = invocation.dispatcher.with_trusted_values(trusted_tool_values)
        result = await run(
            request,
            invocation.model,
            active_dispatcher,
            max_steps=invocation.max_steps,
            hooks=invocation.hooks,
            event_bus=run_events,
            run_id=invocation.run_id,
        )
    finally:
        if entered:
            # 无论 Run 正常、失败或取消，都先清理其所有权资源。
            assert lifecycle is not None
            await lifecycle.after_run(invocation, lifecycle_context)
    await run_events.finish(invocation.run_id)
    return result


def _handle(
    storage: SessionStorage,
    metadata: SessionMetadata,
    revision: int,
    *,
    diagnostics: tuple[str, ...] = (),
) -> SessionHandle:
    """从持久元数据构造不携带预算锚点的 SessionHandle。"""

    return SessionHandle(
        session_id=metadata.id,
        leaf_id=metadata.leaf_id,
        session_file=storage.journal_path(metadata.id),
        metadata=metadata,
        revision=revision,
        diagnostics=diagnostics,
    )


async def _append(
    storage: SessionStorage,
    handle: SessionHandle,
    entries: tuple[Entry, ...],
    *,
    prompt_budget_anchor: PromptBudgetAnchor | None = None,
) -> SessionHandle:
    """把一批相连 Entries 提交到当前 leaf，并返回新 revision Handle。"""

    metadata = handle.metadata.model_copy(
        update={
            "leaf_id": entries[-1].id,
            "updated_at": datetime.now(UTC),
        }
    )
    state = await storage.append_entries(
        handle.session_id,
        expected_revision=handle.revision,
        entries=entries,
        metadata=metadata,
    )
    return _with_runtime(
        _handle(
            storage,
            state.envelope.metadata,
            state.envelope.revision,
            diagnostics=tuple(dict.fromkeys((*handle.diagnostics, *state.diagnostics))),
        ),
        prompt_budget_anchor=prompt_budget_anchor,
    )


def _with_runtime(
    handle: SessionHandle,
    *,
    prompt_budget_anchor: PromptBudgetAnchor | None,
    diagnostics: tuple[str, ...] | None = None,
) -> SessionHandle:
    """替换 SessionHandle 的非持久化运行时字段。"""

    return SessionHandle(
        session_id=handle.session_id,
        leaf_id=handle.leaf_id,
        session_file=handle.session_file,
        metadata=handle.metadata,
        revision=handle.revision,
        prompt_budget_anchor=prompt_budget_anchor,
        diagnostics=handle.diagnostics if diagnostics is None else diagnostics,
    )


def _run_event_bus(
    storage: SessionStorage,
    handle: SessionHandle,
    external: EventEmitter[RunEvent] | None,
) -> tuple[_RunEventBoundary, _SafeStepRecorder, TraceWriter]:
    """组合安全 Step 记录、Trace 与可选外部观察者的 EventBus。"""

    recorder = _SafeStepRecorder()
    trace_writer = TraceWriter(storage.trace_path(handle.session_id))
    listeners: list[EventListener[RunEvent]] = [recorder, trace_writer]
    if external is not None:
        listeners.append(external.emit)
    return _RunEventBoundary(EventBus[RunEvent](listeners)), recorder, trace_writer


async def _cancelled_result(
    storage: SessionStorage,
    handle: SessionHandle,
    recorder: _SafeStepRecorder,
    trace_writer: TraceWriter,
) -> tuple[SessionHandle, RunResult]:
    """在取消后刷出 Trace，并持久化 Event 流中已完整结束的 Steps。"""

    try:
        await trace_writer.flush()
    except Exception:
        # Trace 是尽力而为的观测产品，写入失败不能破坏 Conversation Entries。
        pass
    result = RunResult(RunStatus.CANCELLED, recorder.safe_steps())
    entries = _entries_from_steps(handle.leaf_id, result)
    if entries:
        handle = await _append(storage, handle, entries)
    return handle, result


async def _session_branch_points(
    storage: SessionStorage,
    state: LoadedSession,
) -> set[str]:
    """收集 Session 所有分支中允许选作 leaf/Fork 点的完整消息边界。"""

    referenced = {entry.parent_id for entry in state.entries if entry.parent_id is not None}
    local_leaves = [entry.id for entry in state.entries if entry.id not in referenced]
    if not local_leaves and state.envelope.metadata.fork_point_entry_id is not None:
        local_leaves.append(state.envelope.metadata.fork_point_entry_id)

    branch_points: set[str] = set()
    for leaf_id in local_leaves:
        # 每个 leaf 都可能共享前缀；集合合并得到整棵可导航树的合法边界。
        path = await _materialize_path(
            storage,
            state,
            leaf_id,
            seen_sessions=set(),
        )
        branch_points.update(_validate_path(path, state.envelope.metadata.id))
    return branch_points


def _validate_path(path: tuple[Entry, ...], session_id: str) -> set[str]:
    """校验一条 Entry 路径的消息原子性并返回合法分支点。"""

    positions = {entry.id: index for index, entry in enumerate(path)}
    unit_starts: set[str] = set()
    branch_points: set[str] = set()
    compactions: list[tuple[int, CompactionEntry]] = []
    index = 0
    while index < len(path):
        entry = path[index]
        if isinstance(entry, UserMessageEntry):
            unit_starts.add(entry.id)
            branch_points.add(entry.id)
            index += 1
            continue
        if isinstance(entry, AssistantMessageEntry):
            unit_starts.add(entry.id)
            if not entry.tool_calls:
                branch_points.add(entry.id)
                index += 1
                continue
            expected_ids = [call.id for call in entry.tool_calls]
            # Assistant Tool Calls 与紧随其后的全部 Tool Results 构成不可分割单元。
            following = path[index + 1 : index + 1 + len(expected_ids)]
            if len(following) != len(expected_ids) or not all(
                isinstance(item, ToolResultEntry) for item in following
            ):
                raise CorruptSessionError(
                    session_id,
                    "Assistant Tool Call group is missing a complete Tool Result suffix",
                )
            result_entries = tuple(item for item in following if isinstance(item, ToolResultEntry))
            if [item.result.call_id for item in result_entries] != expected_ids:
                raise CorruptSessionError(
                    session_id,
                    "Assistant Tool Call group has mismatched Tool Results",
                )
            branch_points.add(result_entries[-1].id)
            index += len(result_entries) + 1
            continue
        if isinstance(entry, ToolResultEntry):
            # 独立 Tool Result 会破坏 Model wire message 的调用/结果配对。
            raise CorruptSessionError(
                session_id,
                "Tool Result is not attached to an Assistant Tool Call group",
            )
        if isinstance(entry, CompactionEntry):
            compactions.append((index, entry))
            branch_points.add(entry.id)
        index += 1

    for compaction_index, compaction in compactions:
        # first_kept 必须指向摘要之前某个完整消息单元的起点。
        first_kept_index = positions.get(compaction.first_kept_entry_id)
        if (
            first_kept_index is None
            or first_kept_index >= compaction_index
            or compaction.first_kept_entry_id not in unit_starts
        ):
            raise CorruptSessionError(
                session_id,
                "Compaction Entry references an invalid retained Entry",
            )
    return branch_points


async def _materialize_path(
    storage: SessionStorage,
    state: LoadedSession,
    leaf_id: str | None,
    *,
    seen_sessions: set[str],
) -> tuple[Entry, ...]:
    """从 leaf 沿 parent_id 上溯，并在 Fork 边界递归接入父 Session 前缀。"""

    metadata = state.envelope.metadata
    if metadata.id in seen_sessions:
        raise SessionLineageCycleError(metadata.id)
    lineage = {*seen_sessions, metadata.id}
    by_id = {entry.id: entry for entry in state.entries}
    local_reversed: list[Entry] = []
    current_id = leaf_id
    seen_entries: set[str] = set()
    while current_id in by_id:
        # 本地节点逆向收集，最后反转成从根到 leaf 的阅读顺序。
        if current_id in seen_entries:
            raise SessionLineageCycleError(metadata.id)
        seen_entries.add(current_id)
        entry = by_id[current_id]
        local_reversed.append(entry)
        current_id = entry.parent_id

    prefix: tuple[Entry, ...] = ()
    if current_id is not None:
        # 只有 Fork 精确命中 fork_point 才能跨 Session 解析外部 parent_id。
        if metadata.origin != "fork" or current_id != metadata.fork_point_entry_id:
            entry_id = local_reversed[-1].id if local_reversed else current_id
            raise MissingEntryParentError(metadata.id, entry_id, current_id)
        assert metadata.parent_session_id is not None
        parent = await storage.load_state(metadata.parent_session_id)
        prefix = await _materialize_path(
            storage,
            parent,
            metadata.fork_point_entry_id,
            seen_sessions=lineage,
        )
    elif metadata.origin == "fork" and not local_reversed:
        # 尚无本地 Entry 的 Fork，其完整路径就是父 Session 到 fork point 的路径。
        assert metadata.parent_session_id is not None
        parent = await storage.load_state(metadata.parent_session_id)
        prefix = await _materialize_path(
            storage,
            parent,
            metadata.fork_point_entry_id,
            seen_sessions=lineage,
        )
    return (*prefix, *reversed(local_reversed))


def _context_for_view(
    view: ConversationView,
    tools: ToolRegistry,
    stable_instructions: str,
) -> Context:
    """把类型化 Conversation View Entries 转为 Model 可见 Context。"""

    messages: list[dict[str, Any]] = []
    for entry in view.entries:
        # CompactionEntry 不直接成为消息；其摘要已通过 dropped_summary 单独注入。
        if isinstance(entry, UserMessageEntry):
            messages.append({"role": "user", "content": entry.content})
        elif isinstance(entry, AssistantMessageEntry):
            messages.append(
                serialize_assistant_response(
                    ModelResponse(
                        content=entry.content,
                        reasoning=entry.reasoning,
                        tool_calls=list(entry.tool_calls),
                    )
                )
            )
        elif isinstance(entry, ToolResultEntry):
            messages.append(serialize_tool_result(entry.result))
    return build_context(
        stable_instructions=stable_instructions,
        tool_specs=tools.specs(),
        conversation_messages=messages,
        dropped_summary=view.dropped_summary,
    )


async def _compact(
    handle: SessionHandle,
    *,
    storage: SessionStorage,
    settings: Settings,
    model: Model,
    model_info: ModelInfo | None,
    tools: ToolRegistry,
    stable_instructions: str,
    focus: str | None,
    pending_entry_id: str | None,
) -> SessionHandle:
    """选择可丢弃原子单元、请求摘要并持久化 CompactionEntry。"""

    view = await materialize_conversation(storage, handle)
    units = _atomic_units(view.entries, pending_entry_id=pending_entry_id)
    # 纯策略层决定 cut；Session 服务只负责树遍历、Model 调用与 Entry 持久化。
    selection = select_compaction_units(
        units,
        stable_instructions=stable_instructions,
        tool_specs=tools.specs(),
        model_id=view.model,
        model_info=model_info,
        settings=settings.compaction,
    )
    ordinary_request = _context_for_view(view, tools, stable_instructions).to_request(
        model=view.model
    )
    estimate = estimate_prompt_tokens(
        ordinary_request,
        model_id=view.model or "<unresolved>",
        anchor=handle.prompt_budget_anchor if view.model is not None else None,
    )
    summary_request = _summary_request(
        selection.dropped,
        previous_summary=view.dropped_summary,
        focus=focus,
        model_id=view.model,
        max_tokens=selection.summary_max_tokens,
    )
    summary_input_limit = effective_input_limit(model_info, settings.compaction)
    if (
        summary_input_limit is not None
        and estimate_request_tokens(summary_request) > summary_input_limit
    ):
        # 摘要请求自身也必须适配有效输入窗口，否则不能安全开始 Compaction。
        raise ContextCapacityError("compaction summary request exceeds the effective input limit")
    response = await model.request(summary_request)
    # 摘要调用不是 Agent Run：不允许 Model 通过 Tool Calls 引入新的控制循环。
    if response.tool_calls or response.content is None or not response.content.strip():
        raise InvalidCompactionSummaryError(
            "compaction summary must contain non-empty text and no Tool Calls"
        )
    summary = response.content.strip()
    if summary_input_limit is not None:
        # 用真实摘要重建最终请求；空摘要 envelope 的选择估算并不能替代这次复核。
        retained_messages = [
            deepcopy(message) for unit in selection.retained for message in unit.messages
        ]
        rebuilt_request = build_context(
            stable_instructions=stable_instructions,
            tool_specs=tools.specs(),
            conversation_messages=retained_messages,
            dropped_summary=summary,
        ).to_request(model=view.model)
        if estimate_request_tokens(rebuilt_request) > safe_prompt_limit(
            summary_input_limit,
            settings.compaction,
        ):
            raise ContextCapacityError(
                "rebuilt Context exceeds the safe prompt limit after compaction"
            )
    exact_provider_count = (
        estimate.used_provider_anchor
        and handle.prompt_budget_anchor is not None
        and handle.prompt_budget_anchor.request == ordinary_request
        and estimate.tokens == handle.prompt_budget_anchor.prompt_tokens
    )
    # 只有锚点精确描述当前普通请求时，tokens_before 才可标记为 provider 数据。
    compaction = CompactionEntry(
        parent_id=handle.leaf_id,
        summary=summary,
        tokens_before=estimate.tokens,
        tokens_before_source="provider" if exact_provider_count else "estimate",
        first_kept_entry_id=selection.first_kept_entry_id,
    )
    return await _append(storage, handle, (compaction,))


def _atomic_units(
    entries: tuple[Entry, ...],
    *,
    pending_entry_id: str | None,
) -> tuple[AtomicConversationUnit, ...]:
    """把 Entries 分组成 Compaction 不得拆开的消息原子单元。"""

    units: list[AtomicConversationUnit] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if isinstance(entry, UserMessageEntry):
            units.append(
                AtomicConversationUnit(
                    first_entry_id=entry.id,
                    messages=({"role": "user", "content": entry.content},),
                    pending_user=entry.id == pending_entry_id,
                )
            )
            index += 1
            continue
        if isinstance(entry, AssistantMessageEntry):
            messages = [
                serialize_assistant_response(
                    ModelResponse(
                        content=entry.content,
                        reasoning=entry.reasoning,
                        tool_calls=list(entry.tool_calls),
                    )
                )
            ]
            if entry.tool_calls:
                # 一个 Assistant Tool Call 组与其有序 Tool Results 必须整体保留或丢弃。
                expected = [call.id for call in entry.tool_calls]
                results: list[ToolResultEntry] = []
                for following in entries[index + 1 : index + 1 + len(expected)]:
                    if isinstance(following, ToolResultEntry):
                        results.append(following)
                if [item.result.call_id for item in results] != expected:
                    raise CorruptSessionError(
                        "<materialized>",
                        "Assistant Tool Call group is missing an ordered Tool Result",
                    )
                messages.extend(serialize_tool_result(item.result) for item in results)
                index += len(results)
            units.append(
                AtomicConversationUnit(
                    first_entry_id=entry.id,
                    messages=tuple(messages),
                )
            )
            index += 1
            continue
        if isinstance(entry, ToolResultEntry):
            # 路径校验本应更早捕获；这里再次失败关闭，防止生成无效 Context。
            raise CorruptSessionError(
                "<materialized>",
                "Tool Result is not attached to an Assistant Tool Call group",
            )
        index += 1
    return tuple(units)


def _summary_request(
    dropped: tuple[AtomicConversationUnit, ...],
    *,
    previous_summary: str | None,
    focus: str | None,
    model_id: str | None,
    max_tokens: int,
) -> ModelRequest:
    """构造只生成文本摘要、无 Tools 的独立 ModelRequest。"""

    parts = [
        "Summarize the dropped conversation faithfully for use as earlier Context.",
    ]
    if focus is not None and focus.strip():
        parts.append(f"User-requested emphasis: {focus.strip()}")
    if previous_summary is not None:
        # 重新 Compaction 必须连同旧摘要一起概括，否则更早历史会永久丢失。
        parts.append(f"Previous dropped-history summary:\n{previous_summary}")
    dropped_messages = [message for unit in dropped for message in unit.messages]
    parts.append(
        "Newly dropped messages:\n"
        + json.dumps(
            dropped_messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return ModelRequest(
        messages=[
            {
                "role": "system",
                "content": "Produce only a concise textual conversation summary.",
            },
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        tools=[],
        model=model_id,
        max_tokens=max_tokens,
    )


def _entries_from_steps(parent_id: str | None, result: RunResult) -> tuple[Entry, ...]:
    """把 Run 的完整 Steps 线性拆成可持久化 Entries。"""

    entries: list[Entry] = []
    current_parent = parent_id
    for step in result.steps:
        calls = step.response.tool_calls
        if calls and (
            len(calls) != len(step.tool_results)
            or [call.id for call in calls] != [item.call_id for item in step.tool_results]
        ):
            # 在首个不完整 Tool 往返处停止，保持 Session 仅含可重放的消息前缀。
            break
        assistant = AssistantMessageEntry(
            parent_id=current_parent,
            content=step.response.content,
            reasoning=step.response.reasoning,
            tool_calls=tuple(deepcopy(calls)),
        )
        entries.append(assistant)
        current_parent = assistant.id
        for result_item in step.tool_results:
            # 多个 Tool Results 按 Model 提议顺序串成 Entry 链，同时保持 call_id 关联。
            tool_entry = ToolResultEntry(
                parent_id=current_parent,
                result=deepcopy(result_item),
            )
            entries.append(tool_entry)
            current_parent = tool_entry.id
    return tuple(entries)


def _anchor_from_result(
    model_id: str | None,
    result: RunResult,
) -> PromptBudgetAnchor | None:
    """从最终 Step 的 provider Usage 建立仅限运行时的 prompt 预算锚点。"""

    if model_id is None or not result.steps:
        return None
    final_step = result.steps[-1]
    usage = final_step.response.usage
    if usage is None:
        # 缺少 Usage 时不能把本地估算伪装成 provider 报告值。
        return None
    return PromptBudgetAnchor(
        model_id=model_id,
        request=final_step.request,
        local_estimate=estimate_request_tokens(final_step.request),
        prompt_tokens=usage.prompt_tokens,
    )
