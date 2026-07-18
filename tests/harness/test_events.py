from __future__ import annotations

import asyncio

import pytest

from phi.harness import (
    ApprovalDecided,
    CompletionDecision,
    EventBus,
    Hooks,
    ModelCallCompleted,
    ModelCallDelta,
    ModelCallStarted,
    RunEvent,
    RunFinished,
    RunResult,
    RunStarted,
    RunStatus,
    ToolCallCompleted,
    ToolCallStarted,
    run,
)
from phi.model import (
    ContentDelta,
    FinishEvent,
    ModelRequest,
    ModelResponse,
    ReasoningDelta,
    ScriptedModel,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
)
from phi.tools import BYPASS_MODE, RuleBasedApprovalPolicy, ToolDispatcher, ToolRegistry, tool


def dispatcher() -> ToolDispatcher:
    return ToolDispatcher(ToolRegistry(), RuleBasedApprovalPolicy(BYPASS_MODE))


async def test_event_bus_awaits_listeners_in_order_and_isolates_failures() -> None:
    observations: list[tuple[str, int]] = []

    async def first(event: RunEvent) -> None:
        observations.append(("first", event.event_index))

    async def faulty(event: RunEvent) -> None:
        observations.append(("faulty", event.event_index))
        raise RuntimeError("observer failed")

    async def last(event: RunEvent) -> None:
        observations.append(("last", event.event_index))

    result = await run(
        ModelRequest(messages=[]),
        ScriptedModel([ModelResponse(content="Done", finish_reason="stop")]),
        dispatcher(),
        max_steps=1,
        event_bus=EventBus([first, faulty, last]),
        run_id="listeners",
    )

    assert result.status is RunStatus.COMPLETED
    grouped: dict[int, list[str]] = {}
    for listener, event_index in observations:
        grouped.setdefault(event_index, []).append(listener)
    assert grouped
    assert all(order == ["first", "faulty", "last"] for order in grouped.values())


async def test_explicit_no_subscriber_bus_is_a_no_op() -> None:
    result = await run(
        ModelRequest(messages=[]),
        ScriptedModel([ModelResponse(content="", finish_reason=None)]),
        dispatcher(),
        max_steps=1,
        event_bus=EventBus(),
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == ""


async def test_run_emits_exactly_one_terminal_event_for_a_returned_failure() -> None:
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        ScriptedModel([]),
        dispatcher(),
        max_steps=1,
        event_bus=EventBus([collect]),
        run_id="failed-run",
    )

    assert result.status is RunStatus.FAILED
    assert len([event for event in events if isinstance(event, RunStarted)]) == 1
    terminal = [event for event in events if isinstance(event, RunFinished)]
    assert len(terminal) == 1
    assert terminal[0].result.status is result.status
    assert terminal[0].result.steps == result.steps
    assert terminal[0].result.error is not result.error
    assert terminal[0].result.error is not None
    assert isinstance(terminal[0].result.error, RuntimeError)
    assert str(terminal[0].result.error) == str(result.error)
    with pytest.raises(TypeError, match="immutable"):
        terminal[0].result.error.args = ("mutated",)
    with pytest.raises(TypeError, match="immutable"):
        terminal[0].result.error.with_traceback(None)


async def test_unprintable_failure_cannot_prevent_terminal_result_delivery() -> None:
    class UnprintableError(RuntimeError):
        def __init__(self) -> None:
            self.details = {"items": ["original"]}
            super().__init__("hidden")

        def __str__(self) -> str:
            raise RuntimeError("formatting failed")

    error = UnprintableError()
    events: list[RunEvent] = []

    async def fail(_result: RunResult) -> CompletionDecision:
        raise error

    async def collect(event: RunEvent) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        ScriptedModel([ModelResponse(content="Candidate")]),
        dispatcher(),
        max_steps=1,
        hooks=Hooks(before_run_complete=fail),
        event_bus=EventBus([collect]),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is error
    terminal = next(event for event in events if isinstance(event, RunFinished))
    observed_error = terminal.result.error
    assert isinstance(observed_error, UnprintableError)
    assert observed_error is not error
    assert observed_error.__traceback__ is error.__traceback__
    with pytest.raises(TypeError, match="immutable"):
        observed_error.details["items"].append("mutated")


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "missing", "/tmp/absent"),
        OSError(5, "input/output error"),
        ExceptionGroup("grouped", [ValueError("nested")]),
    ],
)
async def test_terminal_event_preserves_common_failure_types(error: Exception) -> None:
    events: list[RunEvent] = []

    async def fail(_result: RunResult) -> CompletionDecision:
        raise error

    async def collect(event: RunEvent) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        ScriptedModel([ModelResponse(content="Candidate")]),
        dispatcher(),
        max_steps=1,
        hooks=Hooks(before_run_complete=fail),
        event_bus=EventBus([collect]),
    )

    terminal = next(event for event in events if isinstance(event, RunFinished))
    observed_error = terminal.result.error
    assert result.error is error
    assert isinstance(observed_error, type(error))
    assert observed_error is not error
    assert observed_error.__traceback__ is error.__traceback__
    if isinstance(error, ExceptionGroup):
        assert isinstance(observed_error, ExceptionGroup)
        assert observed_error.message == error.message
        assert len(observed_error.exceptions) == len(error.exceptions)
        assert isinstance(observed_error.exceptions[0], ValueError)
        assert observed_error.exceptions[0].args == error.exceptions[0].args
    else:
        assert observed_error.args == error.args
    if isinstance(error, OSError) and isinstance(observed_error, OSError):
        assert observed_error.filename == error.filename


async def test_uncopyable_failure_attribute_is_never_shared_with_an_event() -> None:
    class RejectsCopy:
        def __deepcopy__(self, memo: object) -> RejectsCopy:
            del memo
            raise TypeError("copy rejected")

    class ErrorWithPayload(RuntimeError):
        payload: object

    error = ErrorWithPayload("failed")
    error.payload = RejectsCopy()
    events: list[RunEvent] = []

    async def fail(_result: RunResult) -> CompletionDecision:
        raise error

    async def collect(event: RunEvent) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        ScriptedModel([ModelResponse(content="Candidate")]),
        dispatcher(),
        max_steps=1,
        hooks=Hooks(before_run_complete=fail),
        event_bus=EventBus([collect]),
    )

    terminal = next(event for event in events if isinstance(event, RunFinished))
    observed_error = terminal.result.error
    assert result.error is error
    assert isinstance(observed_error, ErrorWithPayload)
    assert observed_error.payload == "RejectsCopy"
    assert observed_error.payload is not error.payload


async def test_self_referential_failure_attribute_cannot_break_terminal_delivery() -> None:
    class ErrorWithPayload(RuntimeError):
        payload: list[object]

    payload: list[object] = []
    payload.append(payload)
    error = ErrorWithPayload("failed")
    error.payload = payload
    events: list[RunEvent] = []

    async def fail(_result: RunResult) -> CompletionDecision:
        raise error

    async def collect(event: RunEvent) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        ScriptedModel([ModelResponse(content="Candidate")]),
        dispatcher(),
        max_steps=1,
        hooks=Hooks(before_run_complete=fail),
        event_bus=EventBus([collect]),
    )

    terminal = next(event for event in events if isinstance(event, RunFinished))
    observed_error = terminal.result.error
    assert result.status is RunStatus.FAILED
    assert result.error is error
    assert isinstance(observed_error, ErrorWithPayload)
    assert observed_error.payload[0] is observed_error.payload
    with pytest.raises(TypeError, match="immutable"):
        observed_error.payload.append("mutated")


async def test_listener_payload_mutation_cannot_change_run_behavior_or_result() -> None:
    received_values: list[str] = []

    @tool(name="record", description="Record a value.")
    async def record(value: str) -> str:
        received_values.append(value)
        return value

    registry = ToolRegistry([record])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("record-1", "record", {"value": "original"})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Done", finish_reason="stop"),
        ]
    )
    observed_events: list[RunEvent] = []

    async def mutate_payload(event: RunEvent) -> None:
        if isinstance(event, ModelCallStarted):
            event.request.messages.clear()
        elif isinstance(event, ModelCallCompleted):
            event.response.tool_calls.clear()
        elif isinstance(event, ToolCallStarted):
            event.call.arguments["value"] = "mutated"
        elif isinstance(event, RunFinished):
            event.result.steps[0].request.messages.clear()

    async def collect(event: RunEvent) -> None:
        observed_events.append(event)

    result = await run(
        ModelRequest(messages=[{"role": "user", "content": "Act"}]),
        model,
        ToolDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE)),
        max_steps=2,
        event_bus=EventBus([mutate_payload, collect]),
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Done"
    assert received_values == ["original"]
    assert result.steps[0].request.messages == [{"role": "user", "content": "Act"}]
    assert result.steps[0].response.tool_calls == [
        ToolCall("record-1", "record", {"value": "original"})
    ]
    with pytest.raises(TypeError, match="immutable"):
        result.steps[0].request.messages.clear()
    started = next(event for event in observed_events if isinstance(event, ModelCallStarted))
    with pytest.raises(TypeError, match="immutable"):
        started.request.messages.append({"role": "user", "content": "mutated"})
    completed = next(event for event in observed_events if isinstance(event, ModelCallCompleted))
    with pytest.raises(TypeError, match="immutable"):
        completed.response.tool_calls.clear()
    tool_started = next(event for event in observed_events if isinstance(event, ToolCallStarted))
    with pytest.raises(TypeError, match="immutable"):
        tool_started.call.arguments["value"] = "mutated"


async def test_listener_cancellation_is_not_isolated_as_an_observer_failure() -> None:
    later_listener_called = False

    async def cancel(_event: RunEvent) -> None:
        raise asyncio.CancelledError

    async def later(_event: RunEvent) -> None:
        nonlocal later_listener_called
        later_listener_called = True

    with pytest.raises(asyncio.CancelledError):
        await run(
            ModelRequest(messages=[]),
            ScriptedModel([ModelResponse(content="unreachable")]),
            dispatcher(),
            max_steps=1,
            event_bus=EventBus([cancel, later]),
        )

    assert later_listener_called is False


async def test_fragmented_tool_stream_has_one_exact_observable_event_order() -> None:
    @tool(name="echo", description="Echo text.")
    async def echo(text: str) -> str:
        return text

    usage = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    first_deltas = [
        ReasoningDelta("reason"),
        ContentDelta("working"),
        ToolCallDelta(0, "echo-1", "echo", '{"te'),
        ToolCallDelta(0, arguments_fragment='xt":"hello"}'),
        FinishEvent("tool_calls", {"phase": "finish"}),
        UsageEvent(usage, {"phase": "usage"}),
    ]
    registry = ToolRegistry([echo])
    model = ScriptedModel(
        [
            first_deltas,
            ModelResponse(content="Done", finish_reason="stop"),
        ]
    )
    events: list[RunEvent] = []

    async def collect(event: RunEvent) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        model,
        ToolDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE)),
        max_steps=2,
        event_bus=EventBus([collect]),
        run_id="fragment-order",
    )

    assert result.status is RunStatus.COMPLETED
    assert [type(event) for event in events] == [
        RunStarted,
        ModelCallStarted,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallCompleted,
        ToolCallStarted,
        ApprovalDecided,
        ToolCallCompleted,
        ModelCallStarted,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallCompleted,
        RunFinished,
    ]
    assert [event.delta for event in events if isinstance(event, ModelCallDelta)][
        :6
    ] == first_deltas
    assert [event.event_index for event in events] == list(range(len(events)))
