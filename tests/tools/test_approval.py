from __future__ import annotations

import pytest

from phi.model import ToolCall
from phi.tools import (
    ACCEPT_EDITS_MODE,
    BYPASS_MODE,
    DEFAULT_MODE,
    PLAN_MODE,
    ApprovalClass,
    ApprovalMode,
    ApprovalRule,
    AskResolution,
    RuleBasedApprovalPolicy,
    RuleDecision,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


def _classified_registry() -> ToolRegistry:
    @tool(name="inspect", description="Inspect.", approval_class=ApprovalClass.READ_ONLY)
    def inspect() -> str:
        return "read"

    @tool(
        name="change",
        description="Change.",
        approval_class=ApprovalClass.MUTATES_WORKSPACE,
    )
    def change() -> str:
        return "changed"

    @tool(name="execute", description="Execute.", approval_class=ApprovalClass.UNCONFINED)
    def execute() -> str:
        return "executed"

    return ToolRegistry([inspect, change, execute])


@pytest.mark.parametrize(
    ("mode", "allowed"),
    [
        (DEFAULT_MODE, {"inspect"}),
        (ACCEPT_EDITS_MODE, {"inspect", "change"}),
        (PLAN_MODE, {"inspect"}),
        (BYPASS_MODE, {"inspect", "change", "execute"}),
    ],
)
async def test_preset_modes_apply_the_documented_approval_matrix(
    mode: ApprovalMode,
    allowed: set[str],
) -> None:
    dispatcher = ToolDispatcher(_classified_registry(), RuleBasedApprovalPolicy(mode))

    results = {
        name: await dispatcher.dispatch(ToolCall(id=name, name=name, arguments={}))
        for name in ("inspect", "change", "execute")
    }

    assert {name for name, result in results.items() if result.error is None} == allowed
    assert all(
        result.error is None or result.error == f"approval_denied: {name}"
        for name, result in results.items()
    )


async def test_deny_takes_precedence_over_a_matching_allow_rule() -> None:
    mode = ApprovalMode(
        name="overlapping",
        rules=(
            ApprovalRule("*", None, RuleDecision.ALLOW),
            ApprovalRule("execute", ApprovalClass.UNCONFINED, RuleDecision.DENY),
        ),
        on_unmatched=RuleDecision.ALLOW,
    )
    dispatcher = ToolDispatcher(_classified_registry(), RuleBasedApprovalPolicy(mode))

    result = await dispatcher.dispatch(ToolCall(id="deny", name="execute", arguments={}))

    assert result.error == "approval_denied: execute"


async def test_headless_unmatched_ask_fails_closed() -> None:
    mode = ApprovalMode(name="headless", rules=(), on_unmatched=RuleDecision.ASK)
    dispatcher = ToolDispatcher(_classified_registry(), RuleBasedApprovalPolicy(mode))

    result = await dispatcher.dispatch(ToolCall(id="headless", name="inspect", arguments={}))

    assert result.error == "approval_denied: inspect"


async def test_allow_for_session_remembers_only_the_tool_name() -> None:
    resolutions = iter((AskResolution.ALLOW_FOR_SESSION, AskResolution.DENY))
    resolver_calls: list[str] = []

    async def resolve(call: ToolCall, _: object) -> AskResolution:
        resolver_calls.append(call.name)
        return next(resolutions)

    mode = ApprovalMode(
        name="ask-all",
        rules=(ApprovalRule("*", None, RuleDecision.ASK),),
        on_unmatched=RuleDecision.DENY,
    )
    dispatcher = ToolDispatcher(
        _classified_registry(),
        RuleBasedApprovalPolicy(mode, resolve),
    )

    first = await dispatcher.dispatch(ToolCall(id="first", name="inspect", arguments={}))
    remembered = await dispatcher.dispatch(ToolCall(id="remembered", name="inspect", arguments={}))
    same_class = await dispatcher.dispatch(ToolCall(id="same-class", name="change", arguments={}))

    assert first.error is None
    assert remembered.error is None
    assert same_class.error == "approval_denied: change"
    assert resolver_calls == ["inspect", "change"]
