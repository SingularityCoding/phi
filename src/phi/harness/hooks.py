from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from phi.tools import ApprovalPolicy

if TYPE_CHECKING:
    from phi.harness.run import RunResult


class RunDecision(StrEnum):
    ACCEPT = "accept"
    RETRY = "retry"


@dataclass(frozen=True)
class CompletionDecision:
    decision: RunDecision
    feedback: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RunDecision):
            raise ValueError("completion decision must be ACCEPT or RETRY")
        if self.decision is RunDecision.RETRY and (
            not isinstance(self.feedback, str) or not self.feedback.strip()
        ):
            raise ValueError("retry completion decisions require non-empty feedback")
        if self.decision is RunDecision.ACCEPT and self.feedback is not None:
            raise ValueError("accepted completion decisions cannot include feedback")


type CompletionHook = Callable[[RunResult], Awaitable[CompletionDecision]]
type MessageInjectionHook = Callable[[], Awaitable[list[str]]]


@dataclass(frozen=True)
class Hooks:
    before_tool_call: ApprovalPolicy | None = None
    before_run_complete: CompletionHook | None = None
    inject_messages: MessageInjectionHook | None = None
