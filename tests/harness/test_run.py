from __future__ import annotations

import asyncio

import pytest

from phi.harness import (
    ApprovalDecided,
    EventBus,
    ModelCallCompleted,
    ModelCallDelta,
    ModelCallStarted,
    RunFinished,
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
    ToolResult,
    Usage,
    UsageEvent,
)
from phi.tools import (
    BYPASS_MODE,
    ApprovalClass,
    ApprovalDecision,
    ApprovalMode,
    ApprovalRule,
    RuleBasedApprovalPolicy,
    RuleDecision,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


def dispatcher() -> ToolDispatcher:
    return ToolDispatcher(ToolRegistry(), RuleBasedApprovalPolicy(BYPASS_MODE))


async def test_run_streams_one_step_to_an_immutable_completed_result() -> None:
    usage = Usage(prompt_tokens=2, completion_tokens=3, total_tokens=5)
    deltas = [
        ReasoningDelta("checking"),
        ContentDelta("hel"),
        ContentDelta("lo"),
        FinishEvent("stop", {"phase": "finish"}),
        UsageEvent(usage, {"phase": "usage"}),
    ]
    model = ScriptedModel([deltas])
    messages = [{"role": "user", "content": "Say hello"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "unused",
                "description": "Not called.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    initial_request = ModelRequest(
        messages=messages,
        tools=tools,
        model="test-model",
        temperature=0.25,
        max_tokens=100,
    )
    events = []

    async def record(event: object) -> None:
        events.append(event)

    result = await run(
        initial_request,
        model,
        dispatcher(),
        max_steps=1,
        hooks=None,
        event_bus=EventBus([record]),
        run_id="run-1",
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "hello"
    assert result.error is None
    assert isinstance(result.steps, tuple)
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.index == 0
    assert step.request == initial_request
    assert step.response.content == "hello"
    assert step.response.reasoning == "checking"
    assert step.response.usage == usage
    assert step.tool_results == ()
    assert model.requests == [step.request]

    assert [type(event) for event in events] == [
        RunStarted,
        ModelCallStarted,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallDelta,
        ModelCallCompleted,
        RunFinished,
    ]
    assert [event.event_index for event in events] == list(range(len(events)))
    assert {event.run_id for event in events} == {"run-1"}
    assert [event.delta for event in events if isinstance(event, ModelCallDelta)] == deltas
    completed = next(event for event in events if isinstance(event, ModelCallCompleted))
    assert completed.step_index == 0
    assert completed.response == step.response
    assert completed.latency_seconds >= 0
    assert isinstance(events[-1], RunFinished)
    assert events[-1].result == result

    messages[0]["content"] = "mutated"
    tools[0]["function"]["name"] = "mutated"
    assert step.request.messages == [{"role": "user", "content": "Say hello"}]
    assert step.request.tools[0]["function"]["name"] == "unused"


async def test_run_processes_tool_calls_sequentially_and_builds_the_next_request() -> None:
    execution_order: list[str] = []

    @tool(name="record", description="Record one value.")
    async def record(value: str, position: int) -> dict[str, object]:
        execution_order.append(value)
        return {"position": position, "value": value}

    registry = ToolRegistry([record])
    calls = [
        ToolCall(id="call-b", name="record", arguments={"value": "first", "position": 1}),
        ToolCall(id="call-a", name="record", arguments={"value": "second", "position": 2}),
    ]
    model = ScriptedModel(
        [
            ModelResponse(
                content="I will record both.",
                reasoning="two actions",
                tool_calls=calls,
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Recorded.", finish_reason="stop"),
        ]
    )
    initial_request = ModelRequest(
        messages=[{"role": "system", "content": "Be precise."}],
        tools=registry.specs(),
        model="scripted",
        temperature=0.1,
        max_tokens=50,
    )
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    result = await run(
        initial_request,
        model,
        ToolDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE)),
        max_steps=2,
        event_bus=EventBus([collect]),
        run_id="tool-run",
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Recorded."
    assert execution_order == ["first", "second"]
    assert len(result.steps) == 2
    assert result.steps[0].tool_results == (
        ToolResult(
            call_id="call-b",
            output='{"position": 1, "value": "first"}',
        ),
        ToolResult(
            call_id="call-a",
            output='{"position": 2, "value": "second"}',
        ),
    )
    assert result.steps[1].tool_results == ()
    assert model.requests == [result.steps[0].request, result.steps[1].request]
    assert model.requests[1] == ModelRequest(
        messages=[
            {"role": "system", "content": "Be precise."},
            {
                "role": "assistant",
                "content": "I will record both.",
                "reasoning_content": "two actions",
                "tool_calls": [
                    {
                        "id": "call-b",
                        "type": "function",
                        "function": {
                            "name": "record",
                            "arguments": '{"position":1,"value":"first"}',
                        },
                    },
                    {
                        "id": "call-a",
                        "type": "function",
                        "function": {
                            "name": "record",
                            "arguments": '{"position":2,"value":"second"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-b",
                "content": '{"position": 1, "value": "first"}',
            },
            {
                "role": "tool",
                "tool_call_id": "call-a",
                "content": '{"position": 2, "value": "second"}',
            },
        ],
        tools=initial_request.tools,
        model="scripted",
        temperature=0.1,
        max_tokens=50,
    )

    tool_events = [
        event
        for event in events
        if isinstance(event, (ToolCallStarted, ApprovalDecided, ToolCallCompleted))
    ]
    assert [type(event) for event in tool_events] == [
        ToolCallStarted,
        ApprovalDecided,
        ToolCallCompleted,
        ToolCallStarted,
        ApprovalDecided,
        ToolCallCompleted,
    ]
    assert [event.call.id for event in tool_events] == [
        "call-b",
        "call-b",
        "call-b",
        "call-a",
        "call-a",
        "call-a",
    ]
    approvals = [event for event in tool_events if isinstance(event, ApprovalDecided)]
    assert [event.decision for event in approvals] == [
        ApprovalDecision.ALLOW,
        ApprovalDecision.ALLOW,
    ]
    assert [event.mode for event in approvals] == ["bypass", "bypass"]
    completions = [event for event in tool_events if isinstance(event, ToolCallCompleted)]
    assert all(event.latency_seconds >= 0 for event in completions)


async def test_expected_tool_failures_are_returned_to_the_model() -> None:
    @tool(name="bounded", description="Require an integer.")
    async def bounded(count: int) -> str:
        return str(count)

    @tool(
        name="denied",
        description="Require approval.",
        approval_class=ApprovalClass.MUTATES_WORKSPACE,
    )
    async def denied() -> str:
        pytest.fail("a denied Tool must not execute")

    @tool(name="slow", description="Time out.", timeout_seconds=0.01)
    async def slow() -> str:
        await asyncio.Event().wait()
        return "unreachable"

    @tool(name="broken", description="Raise.")
    async def broken() -> str:
        raise LookupError("broken handler")

    registry = ToolRegistry([bounded, denied, slow, broken])
    calls = [
        ToolCall(id="unknown", name="missing", arguments={}),
        ToolCall(id="invalid", name="bounded", arguments={"count": "3"}),
        ToolCall(id="denied", name="denied", arguments={}),
        ToolCall(id="timeout", name="slow", arguments={}),
        ToolCall(id="handler", name="broken", arguments={}),
    ]
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=calls, finish_reason="tool_calls"),
            ModelResponse(content="Recovered", finish_reason="stop"),
        ]
    )
    mixed_mode = ApprovalMode(
        name="mixed",
        rules=(ApprovalRule("denied", None, RuleDecision.DENY),),
        on_unmatched=RuleDecision.ALLOW,
    )
    observed_events = []

    async def collect(event: object) -> None:
        observed_events.append(event)

    result = await run(
        ModelRequest(messages=[{"role": "user", "content": "Use tools"}]),
        model,
        ToolDispatcher(registry, RuleBasedApprovalPolicy(mixed_mode)),
        max_steps=2,
        event_bus=EventBus([collect]),
        run_id="failure-data",
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Recovered"
    tool_results = result.steps[0].tool_results
    assert [item.call_id for item in tool_results] == [
        "unknown",
        "invalid",
        "denied",
        "timeout",
        "handler",
    ]
    assert tool_results[0].error == "unknown_tool: missing"
    assert tool_results[1].error is not None
    assert tool_results[1].error.startswith("invalid_arguments:")
    assert tool_results[2].error == "approval_denied: denied"
    assert tool_results[3].error == "tool_timeout: exceeded 0.01 seconds"
    assert tool_results[4].error == "handler_error: LookupError: broken handler"
    assert [message["tool_call_id"] for message in model.requests[1].messages[-5:]] == [
        result.call_id for result in tool_results
    ]
    assert [message["content"] for message in model.requests[1].messages[-5:]] == [
        result.error for result in tool_results
    ]

    approvals = [event for event in observed_events if isinstance(event, ApprovalDecided)]
    assert [event.call.name for event in approvals] == ["denied", "slow", "broken"]
    assert [event.decision for event in approvals] == [
        ApprovalDecision.DENY,
        ApprovalDecision.ALLOW,
        ApprovalDecision.ALLOW,
    ]
    assert [event.mode for event in approvals] == ["mixed", "mixed", "mixed"]


async def test_run_executes_final_step_tool_calls_before_stopping_at_max_steps() -> None:
    executions: list[str] = []

    @tool(name="act", description="Perform one action.")
    async def act() -> str:
        executions.append("acted")
        return "done"

    registry = ToolRegistry([act])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="act-1", name="act", arguments={})],
                finish_reason="tool_calls",
            )
        ]
    )
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        model,
        ToolDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE)),
        max_steps=1,
        event_bus=EventBus([collect]),
    )

    assert result.status is RunStatus.MAX_STEPS
    assert result.output is None
    assert result.error is None
    assert executions == ["acted"]
    assert len(model.requests) == 1
    assert len(result.steps) == 1
    assert result.steps[0].tool_results == (ToolResult("act-1", "done"),)
    assert isinstance(events[-1], RunFinished)
    assert events[-1].result == result
