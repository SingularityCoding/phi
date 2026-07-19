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
    ContextInspection,
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
from phi.sessions.metadata import SessionMetadata
from phi.sessions.storage import LoadedSession, SessionStorage
from phi.sessions.trace import TraceWriter
from phi.settings import Settings
from phi.tools import ToolDispatcher, ToolRegistry


@dataclass(frozen=True)
class SessionHandle:
    session_id: str
    leaf_id: str | None
    session_file: Path
    metadata: SessionMetadata
    revision: int
    prompt_budget_anchor: PromptBudgetAnchor | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationView:
    session_id: str
    leaf_id: str | None
    entries: tuple[Entry, ...]
    model: str | None
    dropped_summary: str | None = None


@dataclass(frozen=True)
class RunInvocation:
    """Dependencies bound to one exact Harness Run for lifecycle extensions."""

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
    """Generic service-boundary interception for exact Run lifetime ownership."""

    async def before_run(
        self,
        invocation: RunInvocation,
        context: object | None,
    ) -> Mapping[str, object]: ...

    async def after_run(
        self,
        invocation: RunInvocation,
        context: object | None,
    ) -> None: ...


@dataclass
class _ObservedStep:
    index: int
    request: ModelRequest | None = None
    response: ModelResponse | None = None
    tool_results: list[ToolResult] | None = None


@dataclass
class _ActiveSend:
    handle: SessionHandle
    run_id: str | None = None


class _RunEventBoundary:
    """Hold terminal notifications until Session-owned lifecycle cleanup finishes."""

    def __init__(self, event_bus: EventBus[RunEvent]) -> None:
        self._event_bus = event_bus
        self._terminal_events: dict[str, RunFinished] = {}
        self._next_indexes: dict[str, int] = {}

    async def emit(self, event: RunEvent) -> None:
        self._next_indexes[event.run_id] = event.event_index + 1
        if isinstance(event, RunFinished):
            self._terminal_events[event.run_id] = event
            return
        await self._event_bus.emit(event)

    async def finish(self, run_id: str) -> None:
        event = self._terminal_events.pop(run_id, None)
        if event is not None:
            await self._event_bus.emit(event)
        self._next_indexes.pop(run_id, None)

    async def cancel(self, run_id: str, result: RunResult) -> None:
        pending = self._terminal_events.pop(run_id, None)
        next_index = self._next_indexes.pop(run_id, None)
        if pending is None and next_index is None:
            return
        event_index = pending.event_index if pending is not None else next_index
        assert event_index is not None
        await self._event_bus.emit(RunFinished(run_id, event_index, result))


class _SafeStepRecorder:
    def __init__(self) -> None:
        self._run_order: list[str] = []
        self._steps: dict[tuple[str, int], _ObservedStep] = {}

    def __call__(self, event: RunEvent) -> None:
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
                if observed.request is None or observed.response is None:
                    break
                results = tuple(observed.tool_results or ())
                calls = observed.response.tool_calls
                if calls and (
                    len(calls) != len(results)
                    or [call.id for call in calls] != [result.call_id for result in results]
                ):
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
    sessions: list[SessionMetadata] = []
    for envelope in await storage.list_metadata():
        sessions.append((await resume_session(storage, envelope.metadata.id)).metadata)
    return sessions


async def list_leaves(
    storage: SessionStorage,
    handle: SessionHandle,
) -> tuple[str, ...]:
    state = await storage.load_state(handle.session_id)
    await _session_branch_points(storage, state)
    if not state.entries:
        fork_point = state.envelope.metadata.fork_point_entry_id
        return (fork_point,) if fork_point is not None else ()
    referenced = {entry.parent_id for entry in state.entries if entry.parent_id is not None}
    return tuple(entry.id for entry in state.entries if entry.id not in referenced)


async def switch_leaf(
    storage: SessionStorage,
    handle: SessionHandle,
    entry_id: str,
) -> SessionHandle:
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
    state = await storage.load_state(handle.session_id)
    path = await _materialize_path(
        storage,
        state,
        handle.leaf_id,
        seen_sessions=set(),
    )
    _validate_path(path, handle.session_id)
    dropped_summary: str | None = None
    visible: tuple[Entry, ...] = path
    compaction_indexes = [
        index for index, entry in enumerate(path) if isinstance(entry, CompactionEntry)
    ]
    if compaction_indexes:
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


async def inspect_context(
    storage: SessionStorage,
    handle: SessionHandle,
    *,
    settings: Settings,
    model_info: ModelInfo | None,
    tools: ToolRegistry,
    stable_instructions: str,
) -> ContextInspection:
    view = await materialize_conversation(storage, handle)
    context = _context_for_view(view, tools, stable_instructions)
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
        diagnostics = ("Model input limit is unknown; proactive Context budgeting is best-effort",)
    else:
        safe_limit = safe_prompt_limit(effective_limit, settings.compaction)
    return ContextInspection(
        context=context,
        request=request,
        estimate=estimate,
        effective_input_limit=effective_limit,
        safe_prompt_limit=safe_limit,
        diagnostics=diagnostics,
    )


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
    if not text.strip():
        raise ValueError("User messages must not be empty")
    if handle.metadata.model is None and settings.default_model:
        handle = await select_model(storage, handle, settings.default_model)
    await materialize_conversation(storage, handle)
    selected_model = handle.metadata.model or settings.default_model or None
    user_entry = UserMessageEntry(parent_id=handle.leaf_id, content=text)
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
    handle = active.handle
    inspection = await inspect_context(
        storage,
        handle,
        settings=settings,
        model_info=model_info,
        tools=tools,
        stable_instructions=stable_instructions,
    )
    request = inspection.context.to_request(model=selected_model)
    compacted = False
    policy = settings.compaction
    effective_limit = effective_input_limit(model_info, policy)
    if effective_limit is None:
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
                stable_instructions=stable_instructions,
            )
            request = inspection.context.to_request(model=selected_model)
            rebuilt = estimate_prompt_tokens(
                request,
                model_id=selected_model or "<unresolved>",
            )
            if rebuilt.tokens > safe_limit:
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
            stable_instructions=stable_instructions,
        )
        retry_request = retry_inspection.context.to_request(model=selected_model)
        retry_invocation = replace(invocation, run_id=str(uuid4()))
        active.run_id = retry_invocation.run_id
        result = await _execute_run(
            retry_request,
            invocation=retry_invocation,
            run_events=run_events,
            lifecycle=lifecycle,
            lifecycle_context=lifecycle_context,
        )

    completed_entries = _entries_from_steps(handle.leaf_id, result)
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
    trusted_tool_values: Mapping[str, object] = {}
    entered = False
    if lifecycle is not None:
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
    try:
        await trace_writer.flush()
    except Exception:
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
    referenced = {entry.parent_id for entry in state.entries if entry.parent_id is not None}
    local_leaves = [entry.id for entry in state.entries if entry.id not in referenced]
    if not local_leaves and state.envelope.metadata.fork_point_entry_id is not None:
        local_leaves.append(state.envelope.metadata.fork_point_entry_id)

    branch_points: set[str] = set()
    for leaf_id in local_leaves:
        path = await _materialize_path(
            storage,
            state,
            leaf_id,
            seen_sessions=set(),
        )
        branch_points.update(_validate_path(path, state.envelope.metadata.id))
    return branch_points


def _validate_path(path: tuple[Entry, ...], session_id: str) -> set[str]:
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
            raise CorruptSessionError(
                session_id,
                "Tool Result is not attached to an Assistant Tool Call group",
            )
        if isinstance(entry, CompactionEntry):
            compactions.append((index, entry))
            branch_points.add(entry.id)
        index += 1

    for compaction_index, compaction in compactions:
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
    metadata = state.envelope.metadata
    if metadata.id in seen_sessions:
        raise SessionLineageCycleError(metadata.id)
    lineage = {*seen_sessions, metadata.id}
    by_id = {entry.id: entry for entry in state.entries}
    local_reversed: list[Entry] = []
    current_id = leaf_id
    seen_entries: set[str] = set()
    while current_id in by_id:
        if current_id in seen_entries:
            raise SessionLineageCycleError(metadata.id)
        seen_entries.add(current_id)
        entry = by_id[current_id]
        local_reversed.append(entry)
        current_id = entry.parent_id

    prefix: tuple[Entry, ...] = ()
    if current_id is not None:
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
    messages: list[dict[str, Any]] = []
    for entry in view.entries:
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
    view = await materialize_conversation(storage, handle)
    units = _atomic_units(view.entries, pending_entry_id=pending_entry_id)
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
        raise ContextCapacityError("compaction summary request exceeds the effective input limit")
    response = await model.request(summary_request)
    if response.tool_calls or response.content is None or not response.content.strip():
        raise InvalidCompactionSummaryError(
            "compaction summary must contain non-empty text and no Tool Calls"
        )
    summary = response.content.strip()
    if summary_input_limit is not None:
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
    parts = [
        "Summarize the dropped conversation faithfully for use as earlier Context.",
    ]
    if focus is not None and focus.strip():
        parts.append(f"User-requested emphasis: {focus.strip()}")
    if previous_summary is not None:
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
    entries: list[Entry] = []
    current_parent = parent_id
    for step in result.steps:
        calls = step.response.tool_calls
        if calls and (
            len(calls) != len(step.tool_results)
            or [call.id for call in calls] != [item.call_id for item in step.tool_results]
        ):
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
    if model_id is None or not result.steps:
        return None
    final_step = result.steps[-1]
    usage = final_step.response.usage
    if usage is None:
        return None
    return PromptBudgetAnchor(
        model_id=model_id,
        request=final_step.request,
        local_estimate=estimate_request_tokens(final_step.request),
        prompt_tokens=usage.prompt_tokens,
    )
