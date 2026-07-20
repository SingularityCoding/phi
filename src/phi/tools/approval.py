"""用确定性规则和可选的人机交互解析 Tool Call 的审批结果。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Protocol, runtime_checkable

from phi.model import ToolCall
from phi.tools.types import ApprovalClass, Tool


class ApprovalDecision(StrEnum):
    """审批策略交给 dispatcher 的最终二元决定。"""

    ALLOW = "allow"
    DENY = "deny"


class RuleDecision(StrEnum):
    """单条审批规则的配置结果；``ASK`` 仍需 Host 解析。"""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class AskResolution(StrEnum):
    """用户对一次交互式审批请求可作出的选择。"""

    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_FOR_SESSION = "allow_for_session"


class ApprovalPolicy(Protocol):
    """Harness 用于判定某个 Tool Call 是否可执行的策略边界。"""

    async def decide(self, call: ToolCall, tool: Tool) -> ApprovalDecision:
        """返回已经解析完成的允许或拒绝决定。"""
        ...


@runtime_checkable
class ApprovalModeProvider(Protocol):
    """让 Event 观察者可选地读取当前审批模式名称。"""

    @property
    def approval_mode_name(self) -> str:
        """返回用于诊断和 Trace 的稳定模式名称。"""
        ...


ApprovalResolver = Callable[[ToolCall, Tool], Awaitable[AskResolution]]


@dataclass(frozen=True)
class ApprovalRule:
    """同时按 Tool 名称模式和权限类别筛选的一条规则。"""

    tool_pattern: str
    approval_class: ApprovalClass | None
    decision: RuleDecision


@dataclass(frozen=True)
class ApprovalMode:
    """一组有序审批规则及其未匹配时的安全默认值。"""

    name: str
    rules: tuple[ApprovalRule, ...]
    on_unmatched: RuleDecision


def _class_rules(
    read_only: RuleDecision,
    mutates_workspace: RuleDecision,
    unconfined: RuleDecision,
) -> tuple[ApprovalRule, ...]:
    """为三个权限类别生成名称通配的基础规则组。"""

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
HEADLESS_MODE = ApprovalMode(
    "headless",
    _class_rules(RuleDecision.ALLOW, RuleDecision.DENY, RuleDecision.DENY),
    RuleDecision.DENY,
)
BYPASS_MODE = ApprovalMode(
    "bypass",
    _class_rules(RuleDecision.ALLOW, RuleDecision.ALLOW, RuleDecision.ALLOW),
    RuleDecision.DENY,
)


class RuleBasedApprovalPolicy:
    """用确定性规则和可选的人工 resolver 解析 Tool Call。"""

    def __init__(self, mode: ApprovalMode, resolver: ApprovalResolver | None = None) -> None:
        """绑定审批模式，并初始化仅存在于本进程的 Session 级许可。"""

        self.mode = mode
        self._resolver = resolver
        self._session_allowances: set[str] = set()

    @property
    def approval_mode_name(self) -> str:
        """返回当前模式名称，供审批 Event 记录。"""

        return self.mode.name

    def set_resolver(self, resolver: ApprovalResolver) -> None:
        """绑定当前交互式 Host 的模态审批边界。"""

        self._resolver = resolver

    async def decide(self, call: ToolCall, tool: Tool) -> ApprovalDecision:
        """按拒绝优先、Session 许可、首条规则的顺序作出最终决定。"""

        # 同时匹配名称和权限类别；保留原顺序，以便第一条非拒绝规则生效。
        matching = tuple(
            rule
            for rule in self.mode.rules
            if fnmatchcase(tool.name, rule.tool_pattern)
            and (rule.approval_class is None or rule.approval_class == tool.approval_class)
        )
        # 拒绝优先防止宽泛的 allow 规则意外覆盖更具体的 deny 规则。
        if any(rule.decision is RuleDecision.DENY for rule in matching):
            return ApprovalDecision.DENY
        # “本 Session 允许”只按 Tool 名称记忆，且永不写入持久化配置。
        if tool.name in self._session_allowances:
            return ApprovalDecision.ALLOW

        decision = matching[0].decision if matching else self.mode.on_unmatched
        if decision is RuleDecision.ALLOW:
            return ApprovalDecision.ALLOW
        if decision is RuleDecision.DENY or self._resolver is None:
            # 无交互 resolver 的 headless 路径必须 fail closed。
            return ApprovalDecision.DENY

        try:
            resolution = await self._resolver(call, tool)
        except asyncio.CancelledError:
            # 取消属于 Run 控制流，不能伪装成一次审批拒绝。
            raise
        except Exception:
            # UI resolver 故障时不授予执行权限。
            return ApprovalDecision.DENY
        if resolution is AskResolution.ALLOW_FOR_SESSION:
            self._session_allowances.add(tool.name)
            return ApprovalDecision.ALLOW
        if resolution is AskResolution.ALLOW_ONCE:
            return ApprovalDecision.ALLOW
        return ApprovalDecision.DENY
