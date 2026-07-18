from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from phi.harness.snapshots import freeze_request
from phi.model import ModelInfo, ModelRequest


class ContextPolicyError(Exception):
    """Base class for typed Context budgeting and compaction failures."""


class ContextCapacityError(ContextPolicyError):
    """A complete mandatory Context cannot fit within the effective input limit."""


class CompactionDisabledError(ContextPolicyError):
    """Compaction was requested while the policy is disabled."""


class NothingToCompactError(ContextPolicyError):
    """No older complete conversation unit is available to summarize."""


class InvalidCompactionSummaryError(ContextPolicyError):
    """The summary Model response cannot safely become durable Context."""


@dataclass(frozen=True)
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    summary_max_tokens: int = 4_096
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
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
    model_id: str
    request: ModelRequest
    local_estimate: int
    prompt_tokens: int

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("prompt anchors require a resolved Model ID")
        if self.local_estimate < 0 or self.prompt_tokens < 0:
            raise ValueError("prompt anchor token counts must be non-negative")
        object.__setattr__(self, "request", freeze_request(self.request))


@dataclass(frozen=True)
class PromptEstimate:
    tokens: int
    local_tokens: int
    used_provider_anchor: bool


@dataclass(frozen=True)
class AtomicConversationUnit:
    first_entry_id: str
    messages: tuple[dict[str, Any], ...]
    pending_user: bool = False


@dataclass(frozen=True)
class CompactionSelection:
    dropped: tuple[AtomicConversationUnit, ...]
    retained: tuple[AtomicConversationUnit, ...]
    first_kept_entry_id: str
    summary_max_tokens: int


def estimate_request_tokens(request: ModelRequest) -> int:
    """Apply Phi's deterministic complete-request Token Estimate policy."""

    canonical = json.dumps(
        {"messages": request.messages, "tools": request.tools},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
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
    local = estimate_request_tokens(request)
    if anchor is None or not _anchor_matches(anchor, request, model_id):
        return PromptEstimate(local, local, False)
    anchored = anchor.prompt_tokens + max(0, local - anchor.local_estimate)
    return PromptEstimate(max(local, anchored), local, True)


def effective_input_limit(
    model_info: ModelInfo | None,
    settings: CompactionSettings,
) -> int | None:
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
    return settings.enabled and estimated_prompt_tokens > safe_limit


def summary_output_limit(
    model_info: ModelInfo | None,
    settings: CompactionSettings,
) -> int:
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
    if not settings.enabled:
        raise CompactionDisabledError("Context compaction is disabled")
    mandatory_count = 2 if units and units[-1].pending_user and len(units) >= 2 else 1
    if len(units) <= mandatory_count:
        raise NothingToCompactError("no older complete conversation unit is available")

    summary_limit = summary_output_limit(model_info, settings)
    retained = list(units[-mandatory_count:])
    effective_limit = effective_input_limit(model_info, settings)
    safe_limit = (
        safe_prompt_limit(effective_limit, settings) if effective_limit is not None else None
    )
    if not _retained_units_fit(
        retained,
        stable_instructions=stable_instructions,
        tool_specs=tool_specs,
        model_id=model_id,
        summary_limit=summary_limit,
        safe_limit=safe_limit,
    ):
        raise ContextCapacityError("the mandatory recent conversation suffix cannot fit")

    base_size = estimate_request_tokens(
        _post_compaction_request(
            (),
            stable_instructions=stable_instructions,
            tool_specs=tool_specs,
            model_id=model_id,
        )
    )
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
    if safe_limit is None:
        return True
    request = _post_compaction_request(
        retained,
        stable_instructions=stable_instructions,
        tool_specs=tool_specs,
        model_id=model_id,
    )
    return estimate_request_tokens(request) + summary_limit <= safe_limit


def _post_compaction_request(
    retained: list[AtomicConversationUnit] | tuple[()],
    *,
    stable_instructions: str,
    tool_specs: list[dict[str, Any]],
    model_id: str | None,
) -> ModelRequest:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": stable_instructions},
        {"role": "system", "content": "Dropped conversation history summary:\n"},
    ]
    for unit in retained:
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
    previous = anchor.request
    return (
        anchor.model_id == model_id
        and previous.tools == candidate.tools
        and len(previous.messages) <= len(candidate.messages)
        and previous.messages == candidate.messages[: len(previous.messages)]
    )
