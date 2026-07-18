from __future__ import annotations

from typing import Any

import pytest

from phi.harness import EventBus, RunFinished, RunStarted, RunStatus, ToolCallCompleted, run
from phi.model import (
    ModelProtocolError,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    ToolCall,
    ToolCallDelta,
    ToolResult,
)
from phi.tools import (
    BYPASS_MODE,
    ApprovalObserver,
    ApprovalPolicy,
    RuleBasedApprovalPolicy,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


def empty_dispatcher() -> ToolDispatcher:
    return ToolDispatcher(ToolRegistry(), RuleBasedApprovalPolicy(BYPASS_MODE))


async def test_scripted_model_exhaustion_fails_with_completed_steps_retained() -> None:
    @tool(name="advance", description="Advance once.")
    async def advance() -> str:
        return "advanced"

    registry = ToolRegistry([advance])
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("advance-1", "advance", {})],
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
        max_steps=2,
        event_bus=EventBus([collect]),
    )

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, RuntimeError)
    assert str(result.error) == "ScriptedModel response script exhausted"
    assert len(result.steps) == 1
    assert result.steps[0].tool_results == (ToolResult("advance-1", "advanced"),)
    assert len(model.requests) == 2
    assert isinstance(events[-1], RunFinished)
    assert events[-1].result.status is result.status
    assert events[-1].result.steps == result.steps
    assert events[-1].result.error is not result.error


async def test_malformed_assembled_output_fails_without_inventing_a_step() -> None:
    model = ScriptedModel([[ToolCallDelta(index=0, id="incomplete", arguments_fragment="{}")]])

    result = await run(
        ModelRequest(messages=[]),
        model,
        empty_dispatcher(),
        max_steps=1,
    )

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, ModelProtocolError)
    assert result.steps == ()


async def test_dispatcher_contract_defect_preserves_response_and_prior_tool_results() -> None:
    @tool(name="works", description="Complete normally.")
    async def works() -> str:
        return "worked"

    registry = ToolRegistry([works])
    defect = RuntimeError("dispatcher contract violated")

    class DefectiveDispatcher(ToolDispatcher):
        async def dispatch(
            self,
            call: ToolCall,
            *,
            approval_policy: ApprovalPolicy | None = None,
            approval_observer: ApprovalObserver | None = None,
        ) -> ToolResult:
            if call.name == "defect":
                raise defect
            return await super().dispatch(
                call,
                approval_policy=approval_policy,
                approval_observer=approval_observer,
            )

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall("works-1", "works", {}),
                    ToolCall("defect-1", "defect", {}),
                ],
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
        DefectiveDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE)),
        max_steps=1,
        event_bus=EventBus([collect]),
    )

    assert result.status is RunStatus.FAILED
    assert result.error is defect
    assert len(result.steps) == 1
    assert result.steps[0].response.tool_calls[1].id == "defect-1"
    assert result.steps[0].tool_results == (ToolResult("works-1", "worked"),)
    completed_calls = [event.call.id for event in events if isinstance(event, ToolCallCompleted)]
    assert completed_calls == ["works-1"]
    assert isinstance(events[-1], RunFinished)


@pytest.mark.parametrize("invalid_budget", [True, False, 0, -1, 1.5, "2"])
async def test_invalid_step_budget_is_rejected_before_run_started(invalid_budget: Any) -> None:
    events = []

    async def collect(event: object) -> None:
        events.append(event)

    with pytest.raises(ValueError, match="positive integer"):
        await run(
            ModelRequest(messages=[]),
            ScriptedModel([ModelResponse(content="unused")]),
            empty_dispatcher(),
            max_steps=invalid_budget,
            event_bus=EventBus([collect]),
        )

    assert not any(isinstance(event, RunStarted) for event in events)
