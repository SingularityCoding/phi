from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Protocol

from phi.model import ToolCall
from phi.tools.types import ApprovalClass, Tool


class ApprovalDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class RuleDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class AskResolution(StrEnum):
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_FOR_SESSION = "allow_for_session"


class ApprovalPolicy(Protocol):
    async def decide(self, call: ToolCall, tool: Tool) -> ApprovalDecision: ...


ApprovalResolver = Callable[[ToolCall, Tool], Awaitable[AskResolution]]


@dataclass(frozen=True)
class ApprovalRule:
    tool_pattern: str
    approval_class: ApprovalClass | None
    decision: RuleDecision


@dataclass(frozen=True)
class ApprovalMode:
    name: str
    rules: tuple[ApprovalRule, ...]
    on_unmatched: RuleDecision


def _class_rules(
    read_only: RuleDecision,
    mutates_workspace: RuleDecision,
    unconfined: RuleDecision,
) -> tuple[ApprovalRule, ...]:
    return (
        ApprovalRule("*", ApprovalClass.READ_ONLY, read_only),
        ApprovalRule("*", ApprovalClass.MUTATES_WORKSPACE, mutates_workspace),
        ApprovalRule("*", ApprovalClass.UNCONFINED, unconfined),
    )


DEFAULT_MODE = ApprovalMode(
    "default",
    _class_rules(RuleDecision.ALLOW, RuleDecision.ASK, RuleDecision.ASK),
    RuleDecision.DENY,
)
ACCEPT_EDITS_MODE = ApprovalMode(
    "accept_edits",
    _class_rules(RuleDecision.ALLOW, RuleDecision.ALLOW, RuleDecision.ASK),
    RuleDecision.DENY,
)
PLAN_MODE = ApprovalMode(
    "plan",
    _class_rules(RuleDecision.ALLOW, RuleDecision.DENY, RuleDecision.DENY),
    RuleDecision.DENY,
)
BYPASS_MODE = ApprovalMode(
    "bypass",
    _class_rules(RuleDecision.ALLOW, RuleDecision.ALLOW, RuleDecision.ALLOW),
    RuleDecision.DENY,
)


class RuleBasedApprovalPolicy:
    """Resolve Tool Calls from deterministic rules and an optional human resolver."""

    def __init__(self, mode: ApprovalMode, resolver: ApprovalResolver | None = None) -> None:
        self.mode = mode
        self._resolver = resolver
        self._session_allowances: set[str] = set()

    async def decide(self, call: ToolCall, tool: Tool) -> ApprovalDecision:
        matching = tuple(
            rule
            for rule in self.mode.rules
            if fnmatchcase(tool.name, rule.tool_pattern)
            and (rule.approval_class is None or rule.approval_class == tool.approval_class)
        )
        if any(rule.decision is RuleDecision.DENY for rule in matching):
            return ApprovalDecision.DENY
        if tool.name in self._session_allowances:
            return ApprovalDecision.ALLOW

        decision = matching[0].decision if matching else self.mode.on_unmatched
        if decision is RuleDecision.ALLOW:
            return ApprovalDecision.ALLOW
        if decision is RuleDecision.DENY or self._resolver is None:
            return ApprovalDecision.DENY

        try:
            resolution = await self._resolver(call, tool)
        except asyncio.CancelledError:
            raise
        except Exception:
            return ApprovalDecision.DENY
        if resolution is AskResolution.ALLOW_FOR_SESSION:
            self._session_allowances.add(tool.name)
            return ApprovalDecision.ALLOW
        if resolution is AskResolution.ALLOW_ONCE:
            return ApprovalDecision.ALLOW
        return ApprovalDecision.DENY
