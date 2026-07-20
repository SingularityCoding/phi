"""定义能够显式改变 Harness 行为的有限 Hook 集合。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from phi.tools import ApprovalPolicy

if TYPE_CHECKING:
    from phi.harness.run import RunResult


class RunDecision(StrEnum):
    """枚举完成 Hook 对临时 Run 结果的处理决定。"""

    ACCEPT = "accept"
    RETRY = "retry"


@dataclass(frozen=True)
class CompletionDecision:
    """描述接受临时结果或携带纠正反馈重试的决定。"""

    decision: RunDecision
    feedback: str | None = None

    def __post_init__(self) -> None:
        """验证决定与反馈之间的状态不变量。"""

        # RETRY 必须给 Model 可执行的纠正输入；ACCEPT 则不能夹带无效反馈。
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
    """汇集 Run 支持的三个显式行为拦截点。

    Event 只能观察；这些 Hook 的返回值才被 Harness 用于审批 Tool Call、接受完成结果，
    或在下一个 Step 边界注入 Steer 消息。
    """

    before_tool_call: ApprovalPolicy | None = None
    before_run_complete: CompletionHook | None = None
    inject_messages: MessageInjectionHook | None = None
