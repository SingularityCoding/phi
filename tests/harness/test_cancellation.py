from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from phi.harness import EventBus, RunEvent, RunFinished, run
from phi.model import (
    ContentDelta,
    ModelEvent,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    ToolCall,
)
from phi.tools import (
    BYPASS_MODE,
    DEFAULT_MODE,
    ApprovalClass,
    AskResolution,
    RuleBasedApprovalPolicy,
    Tool,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


async def test_cancellation_closes_the_active_model_stream_and_propagates() -> None:
    started = asyncio.Event()
    stream_closed = asyncio.Event()
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
                stream_closed.set()

    events: list[RunEvent] = []

    async def collect(event: RunEvent) -> None:
        events.append(event)

    task = asyncio.create_task(
        run(
            ModelRequest(messages=[]),
            BlockingModel(),
            ToolDispatcher(ToolRegistry(), RuleBasedApprovalPolicy(BYPASS_MODE)),
            max_steps=1,
            event_bus=EventBus([collect]),
        )
    )
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream_closed.is_set()
    assert not any(isinstance(event, RunFinished) for event in events)


async def test_cancellation_propagates_through_interactive_approval() -> None:
    approval_started = asyncio.Event()
    approval_cancelled = asyncio.Event()
    never = asyncio.Event()

    @tool(
        name="mutate",
        description="Wait for approval.",
        approval_class=ApprovalClass.MUTATES_WORKSPACE,
    )
    async def mutate() -> str:
        pytest.fail("the Tool must not execute before approval resolves")

    async def resolve(_call: ToolCall, _tool: Tool) -> AskResolution:
        approval_started.set()
        try:
            await never.wait()
            return AskResolution.DENY
        finally:
            approval_cancelled.set()

    registry = ToolRegistry([mutate])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("mutate-1", "mutate", {})],
                finish_reason="tool_calls",
            )
        ]
    )
    events: list[RunEvent] = []

    async def collect(event: RunEvent) -> None:
        events.append(event)

    task = asyncio.create_task(
        run(
            ModelRequest(messages=[]),
            model,
            ToolDispatcher(
                registry,
                RuleBasedApprovalPolicy(DEFAULT_MODE, resolver=resolve),
            ),
            max_steps=1,
            event_bus=EventBus([collect]),
        )
    )
    await approval_started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert approval_cancelled.is_set()
    assert not any(isinstance(event, RunFinished) for event in events)


async def test_cancellation_propagates_into_an_asynchronous_tool() -> None:
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()
    never = asyncio.Event()

    @tool(name="wait", description="Wait asynchronously.")
    async def wait() -> str:
        tool_started.set()
        try:
            await never.wait()
            return "unreachable"
        finally:
            tool_cancelled.set()

    registry = ToolRegistry([wait])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("wait-1", "wait", {})],
                finish_reason="tool_calls",
            )
        ]
    )
    events: list[RunEvent] = []

    async def collect(event: RunEvent) -> None:
        events.append(event)

    task = asyncio.create_task(
        run(
            ModelRequest(messages=[]),
            model,
            ToolDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE)),
            max_steps=1,
            event_bus=EventBus([collect]),
        )
    )
    await tool_started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool_cancelled.is_set()
    assert not any(isinstance(event, RunFinished) for event in events)
