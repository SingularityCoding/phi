from __future__ import annotations

from typing import Any

import pytest

from phi.harness import (
    ApprovalDecided,
    CompletionDecision,
    EventBus,
    Hooks,
    RunDecision,
    RunResult,
    RunStatus,
    run,
)
from phi.model import ModelRequest, ModelResponse, ScriptedModel, ToolCall
from phi.tools import (
    BYPASS_MODE,
    PLAN_MODE,
    ApprovalClass,
    ApprovalDecision,
    RuleBasedApprovalPolicy,
    Tool,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


def dispatcher(registry: ToolRegistry | None = None) -> ToolDispatcher:
    return ToolDispatcher(
        registry or ToolRegistry(),
        RuleBasedApprovalPolicy(BYPASS_MODE),
    )


@pytest.mark.parametrize("invalid_decision", ["accept", "retry", "other", None])
def test_completion_decision_rejects_values_outside_its_enum(invalid_decision: Any) -> None:
    with pytest.raises(ValueError, match="ACCEPT or RETRY"):
        CompletionDecision(invalid_decision)


async def test_completion_hook_retries_with_feedback_under_the_same_budget() -> None:
    model = ScriptedModel(
        [
            ModelResponse(content="Draft", reasoning="first pass", finish_reason="stop"),
            ModelResponse(content="Final", finish_reason="length"),
        ]
    )
    provisional_results: list[RunResult] = []

    async def check_completion(result: RunResult) -> CompletionDecision:
        provisional_results.append(result)
        if len(provisional_results) == 1:
            return CompletionDecision(RunDecision.RETRY, "Include the missing evidence.")
        return CompletionDecision(RunDecision.ACCEPT)

    result = await run(
        ModelRequest(messages=[{"role": "user", "content": "Answer"}]),
        model,
        dispatcher(),
        max_steps=2,
        hooks=Hooks(before_run_complete=check_completion),
        run_id="completion-retry",
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Final"
    assert len(result.steps) == 2
    assert [item.output for item in provisional_results] == ["Draft", "Final"]
    assert [len(item.steps) for item in provisional_results] == [1, 2]
    assert model.requests[1].messages == [
        {"role": "user", "content": "Answer"},
        {
            "role": "assistant",
            "content": "Draft",
            "reasoning_content": "first pass",
        },
        {"role": "user", "content": "Include the missing evidence."},
    ]


async def test_completion_retry_on_the_final_step_returns_max_steps() -> None:
    model = ScriptedModel([ModelResponse(content="Draft", finish_reason="stop")])

    async def retry(_result: RunResult) -> CompletionDecision:
        return CompletionDecision(RunDecision.RETRY, "Try again.")

    result = await run(
        ModelRequest(messages=[]),
        model,
        dispatcher(),
        max_steps=1,
        hooks=Hooks(before_run_complete=retry),
    )

    assert result.status is RunStatus.MAX_STEPS
    assert result.output is None
    assert len(result.steps) == 1
    assert len(model.requests) == 1


async def test_injected_messages_are_drained_in_order_at_each_step_boundary() -> None:
    @tool(name="continue", description="Advance the Run.")
    async def continue_run() -> str:
        return "continued"

    registry = ToolRegistry([continue_run])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("continue-1", "continue", {})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Done", finish_reason="stop"),
        ]
    )
    injections = iter([["first", "second"], []])

    async def inject() -> list[str]:
        return next(injections)

    result = await run(
        ModelRequest(messages=[{"role": "system", "content": "base"}]),
        model,
        dispatcher(registry),
        max_steps=2,
        hooks=Hooks(inject_messages=inject),
    )

    assert result.status is RunStatus.COMPLETED
    assert model.requests[0].messages == [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    assert model.requests[1].messages[:3] == model.requests[0].messages
    assert model.requests[1].messages[3]["role"] == "assistant"
    assert model.requests[1].messages[4] == {
        "role": "tool",
        "tool_call_id": "continue-1",
        "content": "continued",
    }


async def test_completion_hook_exception_fails_with_the_completed_step_preserved() -> None:
    error = LookupError("completion check failed")
    model = ScriptedModel([ModelResponse(content="Candidate", finish_reason="stop")])

    async def fail(_result: RunResult) -> CompletionDecision:
        raise error

    result = await run(
        ModelRequest(messages=[]),
        model,
        dispatcher(),
        max_steps=1,
        hooks=Hooks(before_run_complete=fail),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is error
    assert result.output is None
    assert len(result.steps) == 1
    assert result.steps[0].response.content == "Candidate"


async def test_tool_hook_overrides_dispatcher_policy_and_reports_its_mode() -> None:
    executed: list[bool] = []

    @tool(
        name="allowed",
        description="Run only through the Hook override.",
        approval_class=ApprovalClass.MUTATES_WORKSPACE,
    )
    async def allowed() -> str:
        executed.append(True)
        return "yes"

    registry = ToolRegistry([allowed])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("allowed-1", "allowed", {})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Done", finish_reason="stop"),
        ]
    )
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        model,
        ToolDispatcher(registry, RuleBasedApprovalPolicy(PLAN_MODE)),
        max_steps=2,
        hooks=Hooks(before_tool_call=RuleBasedApprovalPolicy(BYPASS_MODE)),
        event_bus=EventBus([collect]),
    )

    assert result.status is RunStatus.COMPLETED
    assert executed == [True]
    approvals = [event for event in events if isinstance(event, ApprovalDecided)]
    assert len(approvals) == 1
    assert approvals[0].decision is ApprovalDecision.ALLOW
    assert approvals[0].mode == "bypass"


async def test_generic_tool_hook_reports_no_rule_based_mode() -> None:
    class AllowPolicy:
        async def decide(self, call: ToolCall, tool: Tool) -> ApprovalDecision:
            del call, tool
            return ApprovalDecision.ALLOW

    @tool(name="allowed", description="Use a generic policy.")
    async def allowed() -> str:
        return "yes"

    registry = ToolRegistry([allowed])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("allowed-1", "allowed", {})],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Done", finish_reason="stop"),
        ]
    )
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    result = await run(
        ModelRequest(messages=[]),
        model,
        ToolDispatcher(registry, RuleBasedApprovalPolicy(PLAN_MODE)),
        max_steps=2,
        hooks=Hooks(before_tool_call=AllowPolicy()),
        event_bus=EventBus([collect]),
    )

    assert result.status is RunStatus.COMPLETED
    approval = next(event for event in events if isinstance(event, ApprovalDecided))
    assert approval.decision is ApprovalDecision.ALLOW
    assert approval.mode is None
