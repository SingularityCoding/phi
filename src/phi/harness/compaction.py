"""实现 Context token 预算、锚点估算与 Compaction 切分策略。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from phi.harness.snapshots import freeze_request
from phi.model import ModelInfo, ModelRequest


class ContextPolicyError(Exception):
    """表示 Context 预算与 Compaction 策略失败的类型化基类。"""


class ContextCapacityError(ContextPolicyError):
    """表示必须保留的完整 Context 无法容纳于有效输入限制。"""


class CompactionDisabledError(ContextPolicyError):
    """表示在策略禁用时仍请求了 Compaction。"""


class NothingToCompactError(ContextPolicyError):
    """表示没有可供摘要的更早完整对话单元。"""


class InvalidCompactionSummaryError(ContextPolicyError):
    """表示摘要 Model 响应无法安全地成为持久 Context。"""


@dataclass(frozen=True)
class CompactionSettings:
    """保存可信的 Context Compaction 预算配置。"""

    enabled: bool = True
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    summary_max_tokens: int = 4_096
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
        """在策略边界验证所有容量配置的不变量。"""

        # bool 是 int 的子类，必须显式排除，避免 True 被当成一个 token。
        if not isinstance(self.enabled, bool):
            raise ValueError("compaction enabled must be a boolean")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.reserve_tokens, self.keep_recent_tokens)
        ):
            raise ValueError("compaction reserve and recent targets must be non-negative integers")
        if (
            isinstance(self.summary_max_tokens, bool)
            or not isinstance(self.summary_max_tokens, int)
            or self.summary_max_tokens <= 0
        ):
            raise ValueError("compaction summary maximum must be a positive integer")
        if self.max_input_tokens is not None and (
            isinstance(self.max_input_tokens, bool)
            or not isinstance(self.max_input_tokens, int)
            or self.max_input_tokens <= 0
        ):
            raise ValueError("compaction input maximum must be positive when supplied")


@dataclass(frozen=True)
class PromptBudgetAnchor:
    """保存上一请求的提供方 Usage，用于锚定下一 Context 的估算。"""

    model_id: str
    request: ModelRequest
    local_estimate: int
    prompt_tokens: int

    def __post_init__(self) -> None:
        """校验锚点并冻结请求，避免后续修改破坏前缀比较。"""

        if not self.model_id:
            raise ValueError("prompt anchors require a resolved Model ID")
        if self.local_estimate < 0 or self.prompt_tokens < 0:
            raise ValueError("prompt anchor token counts must be non-negative")
        object.__setattr__(self, "request", freeze_request(self.request))


@dataclass(frozen=True)
class PromptEstimate:
    """描述完整请求的 Token Estimate 及其来源。"""

    tokens: int
    local_tokens: int
    used_provider_anchor: bool


@dataclass(frozen=True)
class AtomicConversationUnit:
    """表示 Compaction 不得拆开的连续对话消息单元。"""

    first_entry_id: str
    messages: tuple[dict[str, Any], ...]
    pending_user: bool = False


@dataclass(frozen=True)
class CompactionSelection:
    """记录本次 Compaction 要摘要与保留的原子单元。"""

    dropped: tuple[AtomicConversationUnit, ...]
    retained: tuple[AtomicConversationUnit, ...]
    first_kept_entry_id: str
    summary_max_tokens: int


def estimate_request_tokens(request: ModelRequest) -> int:
    """对完整请求应用 Phi 的确定性 Token Estimate 策略。"""

    # 稳定键顺序与紧凑分隔符保证同一请求在不同运行中得到相同估算。
    canonical = json.dumps(
        {"messages": request.messages, "tools": request.tools},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # 中文等非 ASCII 字符按每个字符一个 token 保守计数，避免简单除四造成低估。
    ascii_codepoints = sum(ord(character) < 128 for character in canonical)
    non_ascii_codepoints = len(canonical) - ascii_codepoints
    return (
        (ascii_codepoints + 3) // 4
        + non_ascii_codepoints
        + 4 * len(request.messages)
        + 8 * len(request.tools)
        + 16
    )


def estimate_prompt_tokens(
    request: ModelRequest,
    *,
    model_id: str,
    anchor: PromptBudgetAnchor | None = None,
) -> PromptEstimate:
    """估算候选 Context，并在结构前缀不变时利用提供方锚点。"""

    local = estimate_request_tokens(request)
    # 锚点只描述特定 Model 与请求前缀；任一结构变化都会退回纯本地估算。
    if anchor is None or not _anchor_matches(anchor, request, model_id):
        return PromptEstimate(local, local, False)
    anchored = anchor.prompt_tokens + max(0, local - anchor.local_estimate)
    # 取两种估算较大者，确保锚点不会反而降低保守的本地预算。
    return PromptEstimate(max(local, anchored), local, True)


def effective_input_limit(
    model_info: ModelInfo | None,
    settings: CompactionSettings,
) -> int | None:
    """计算提供方容量与本地安全上限中更严格的有效输入限制。"""

    provider_limit = model_info.max_input_tokens if model_info is not None else None
    configured_limit = settings.max_input_tokens
    if provider_limit is None:
        return configured_limit
    if configured_limit is None:
        return provider_limit
    return min(provider_limit, configured_limit)


def safe_prompt_limit(
    effective_limit: int,
    settings: CompactionSettings,
) -> int:
    """从有效输入限制中扣除预留输出预算。"""

    safe_limit = effective_limit - settings.reserve_tokens
    if safe_limit <= 0:
        raise ContextCapacityError(
            "the reserved completion budget leaves no positive prompt capacity"
        )
    return safe_limit


def should_compact(
    estimated_prompt_tokens: int,
    safe_limit: int,
    settings: CompactionSettings,
) -> bool:
    """判断已启用的策略是否因超过安全上限而需要 Compaction。"""

    return settings.enabled and estimated_prompt_tokens > safe_limit


def summary_output_limit(
    model_info: ModelInfo | None,
    settings: CompactionSettings,
) -> int:
    """计算提供方输出容量与摘要配置中更严格的上限。"""

    provider_limit = model_info.max_output_tokens if model_info is not None else None
    if provider_limit is None:
        return settings.summary_max_tokens
    return min(provider_limit, settings.summary_max_tokens)


def select_compaction_units(
    units: tuple[AtomicConversationUnit, ...],
    *,
    stable_instructions: str,
    tool_specs: list[dict[str, Any]],
    model_id: str | None,
    model_info: ModelInfo | None,
    settings: CompactionSettings,
) -> CompactionSelection:
    """选择需要摘要和保留的原子对话单元。

    选择从最新消息向过去扩展，始终保留最新完整单元；若末尾是本次待发送的
    User 消息，还会额外保留它之前的一个完整单元，使摘要后仍有有效上下文。
    """

    if not settings.enabled:
        raise CompactionDisabledError("Context compaction is disabled")
    # 待发送 User 消息本身不提供回答背景，因此与前一个单元共同组成强制后缀。
    mandatory_count = 2 if units and units[-1].pending_user and len(units) >= 2 else 1
    if len(units) <= mandatory_count:
        raise NothingToCompactError("no older complete conversation unit is available")

    summary_limit = summary_output_limit(model_info, settings)
    retained = list(units[-mandatory_count:])
    effective_limit = effective_input_limit(model_info, settings)
    safe_limit = (
        safe_prompt_limit(effective_limit, settings) if effective_limit is not None else None
    )
    # 先验证不可丢弃的后缀；若它已超限，再摘要更早历史也无济于事。
    if not _retained_units_fit(
        retained,
        stable_instructions=stable_instructions,
        tool_specs=tool_specs,
        model_id=model_id,
        summary_limit=summary_limit,
        safe_limit=safe_limit,
    ):
        raise ContextCapacityError("the mandatory recent conversation suffix cannot fit")

    # base_size 排除历史，只保留稳定指令、工具和摘要占位，用来衡量近期历史净大小。
    base_size = estimate_request_tokens(
        _post_compaction_request(
            (),
            stable_instructions=stable_instructions,
            tool_specs=tool_specs,
            model_id=model_id,
        )
    )
    # 从近到远贪心扩展，既满足 keep_recent_tokens 目标，也不越过安全 Context 上限。
    for older in reversed(units[:-mandatory_count]):
        current_size = estimate_request_tokens(
            _post_compaction_request(
                retained,
                stable_instructions=stable_instructions,
                tool_specs=tool_specs,
                model_id=model_id,
            )
        )
        if current_size - base_size >= settings.keep_recent_tokens:
            break
        candidate = [older, *retained]
        if not _retained_units_fit(
            candidate,
            stable_instructions=stable_instructions,
            tool_specs=tool_specs,
            model_id=model_id,
            summary_limit=summary_limit,
            safe_limit=safe_limit,
        ):
            break
        retained = candidate

    # 至少必须真正丢弃一个完整单元，否则发起摘要请求没有意义。
    dropped_count = len(units) - len(retained)
    if dropped_count <= 0:
        raise NothingToCompactError("recent-history target leaves nothing to summarize")
    return CompactionSelection(
        dropped=units[:dropped_count],
        retained=tuple(retained),
        first_kept_entry_id=retained[0].first_entry_id,
        summary_max_tokens=summary_limit,
    )


def _retained_units_fit(
    retained: list[AtomicConversationUnit],
    *,
    stable_instructions: str,
    tool_specs: list[dict[str, Any]],
    model_id: str | None,
    summary_limit: int,
    safe_limit: int | None,
) -> bool:
    """判断保留单元与最坏情况摘要能否同时放入安全 Context。"""

    if safe_limit is None:
        return True
    request = _post_compaction_request(
        retained,
        stable_instructions=stable_instructions,
        tool_specs=tool_specs,
        model_id=model_id,
    )
    # 为摘要预留完整输出上限，保证摘要实际写入后仍有容量余量。
    return estimate_request_tokens(request) + summary_limit <= safe_limit


def _post_compaction_request(
    retained: list[AtomicConversationUnit] | tuple[()],
    *,
    stable_instructions: str,
    tool_specs: list[dict[str, Any]],
    model_id: str | None,
) -> ModelRequest:
    """构造切分后、摘要内容暂为空的精确 Model 请求形状。"""

    # 空摘要占位仍参与估算，确保角色和固定前缀开销不会被漏算。
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": stable_instructions},
        {"role": "system", "content": "Dropped conversation history summary:\n"},
    ]
    for unit in retained:
        # 深拷贝保持纯策略函数不与调用方持有的可变 wire 字典共享状态。
        messages.extend(deepcopy(unit.messages))
    return ModelRequest(
        messages=messages,
        tools=deepcopy(tool_specs),
        model=model_id,
    )


def _anchor_matches(
    anchor: PromptBudgetAnchor,
    candidate: ModelRequest,
    model_id: str,
) -> bool:
    """判断锚点请求是否为同一 Model 候选请求的结构前缀。"""

    previous = anchor.request
    return (
        anchor.model_id == model_id
        and previous.tools == candidate.tools
        and len(previous.messages) <= len(candidate.messages)
        and previous.messages == candidate.messages[: len(previous.messages)]
    )
