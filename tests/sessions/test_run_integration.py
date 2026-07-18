from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from phi.harness import RunStatus
from phi.model import (
    ContentDelta,
    FinishEvent,
    ModelEvent,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    ToolCall,
    ToolCallDelta,
)
from phi.sessions import (
    AssistantMessageEntry,
    SessionStorage,
    ToolResultEntry,
    UserMessageEntry,
    create_session,
    materialize_conversation,
    resume_session,
    send_message,
)
from phi.settings import Settings
from phi.tools import (
    BYPASS_MODE,
    Injected,
    RuleBasedApprovalPolicy,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


async def test_multistep_tool_run_persists_only_complete_message_level_units(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("missing-1", "missing", {})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="recovered"),
        ]
    )

    updated, result = await send_message(
        handle,
        "use a tool",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=2,
    )

    resumed = await resume_session(SessionStorage(tmp_path), updated.session_id)
    view = await materialize_conversation(SessionStorage(tmp_path), resumed)
    assert result.status is RunStatus.COMPLETED
    assert [type(entry) for entry in view.entries] == [
        UserMessageEntry,
        AssistantMessageEntry,
        ToolResultEntry,
        AssistantMessageEntry,
    ]
    tool_entry = view.entries[2]
    assert isinstance(tool_entry, ToolResultEntry)
    assert tool_entry.result.error == "unknown_tool: missing"
    assert model.requests[1].messages[-1] == {
        "role": "tool",
        "tool_call_id": "missing-1",
        "content": "unknown_tool: missing",
    }


async def test_max_step_run_persists_its_complete_tool_unit(tmp_path) -> None:
    @tool(name="finish", description="Finish one action.")
    async def finish() -> str:
        return "finished"

    storage = SessionStorage(tmp_path)
    tools = ToolRegistry([finish])
    handle = await create_session(storage, model="model-a")
    updated, result = await send_message(
        handle,
        "finish",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(tool_calls=[ToolCall("finish-1", "finish", {})])]),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )

    view = await materialize_conversation(storage, updated)
    assert result.status is RunStatus.MAX_STEPS
    assert [type(entry) for entry in view.entries] == [
        UserMessageEntry,
        AssistantMessageEntry,
        ToolResultEntry,
    ]


async def test_dispatcher_defect_does_not_persist_a_partial_tool_group(tmp_path) -> None:
    @tool(name="broken", description="Require missing trusted state.")
    async def broken(value: Injected[str]) -> str:
        return value

    storage = SessionStorage(tmp_path)
    tools = ToolRegistry([broken])
    handle = await create_session(storage, model="model-a")
    updated, result = await send_message(
        handle,
        "break",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(tool_calls=[ToolCall("broken-1", "broken", {})])]),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )

    view = await materialize_conversation(storage, updated)
    assert result.status is RunStatus.FAILED
    assert len(result.steps) == 1
    assert result.steps[0].tool_results == ()
    assert [type(entry) for entry in view.entries] == [UserMessageEntry]


async def test_session_service_translates_cancellation_and_keeps_the_user_entry(tmp_path) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()
    never = asyncio.Event()

    class BlockingModel:
        async def request(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError(f"ordinary Runs must stream: {request}")

        async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            del request
            started.set()
            try:
                await never.wait()
                yield ContentDelta("unreachable")
            finally:
                closed.set()

    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a")
    task = asyncio.create_task(
        send_message(
            handle,
            "do not lose this",
            storage=storage,
            settings=Settings(),
            model=BlockingModel(),
            model_info=None,
            tools=tools,
            dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
            stable_instructions="stable",
            max_steps=1,
        )
    )
    await started.wait()

    task.cancel()
    cancelled_handle, result = await task

    assert result.status is RunStatus.CANCELLED
    assert closed.is_set()
    view = await materialize_conversation(storage, cancelled_handle)
    assert len(view.entries) == 1
    assert isinstance(view.entries[0], UserMessageEntry)
    assert view.entries[0].content == "do not lose this"


async def test_session_service_translates_cancellation_during_threshold_compaction(
    tmp_path,
) -> None:
    summary_started = asyncio.Event()
    summary_closed = asyncio.Event()
    never = asyncio.Event()

    class BlockingCompactionModel:
        async def request(self, request: ModelRequest) -> ModelResponse:
            del request
            summary_started.set()
            try:
                await never.wait()
            finally:
                summary_closed.set()
            raise AssertionError("unreachable")

        async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            raise AssertionError(f"ordinary Run must not start before compaction: {request}")
            yield ContentDelta("unreachable")

    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("x" * 300, "y" * 300), ("short", "brief")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    task = asyncio.create_task(
        send_message(
            handle,
            "new",
            storage=storage,
            settings=Settings(
                compaction_reserve_tokens=180,
                compaction_keep_recent_tokens=0,
                compaction_summary_max_tokens=20,
            ),
            model=BlockingCompactionModel(),
            model_info=ModelInfo("model-a", max_input_tokens=400),
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    )
    await summary_started.wait()

    task.cancel()
    cancelled_handle, result = await task

    view = await materialize_conversation(storage, cancelled_handle)
    assert result.status is RunStatus.CANCELLED
    assert summary_closed.is_set()
    assert isinstance(view.entries[-1], UserMessageEntry)
    assert view.entries[-1].content == "new"


async def test_cancellation_persists_a_complete_tool_unit_but_not_the_active_step(tmp_path) -> None:
    second_call_started = asyncio.Event()
    never = asyncio.Event()

    @tool(name="finish", description="Finish one complete action.")
    async def finish() -> str:
        return "finished"

    class TwoStepBlockingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def request(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError(f"ordinary Runs must stream: {request}")

        async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            del request
            self.calls += 1
            if self.calls == 1:
                yield ToolCallDelta(index=0, id="finish-1", name="finish", arguments_fragment="{}")
                yield FinishEvent("tool_calls", {})
                return
            second_call_started.set()
            await never.wait()
            yield ContentDelta("unreachable")

    storage = SessionStorage(tmp_path)
    tools = ToolRegistry([finish])
    handle = await create_session(storage, model="model-a")
    task = asyncio.create_task(
        send_message(
            handle,
            "finish then wait",
            storage=storage,
            settings=Settings(),
            model=TwoStepBlockingModel(),
            model_info=None,
            tools=tools,
            dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
            stable_instructions="stable",
            max_steps=2,
        )
    )
    await second_call_started.wait()

    task.cancel()
    cancelled_handle, result = await task
    view = await materialize_conversation(storage, cancelled_handle)

    assert result.status is RunStatus.CANCELLED
    assert len(result.steps) == 1
    assert [type(entry) for entry in view.entries] == [
        UserMessageEntry,
        AssistantMessageEntry,
        ToolResultEntry,
    ]
