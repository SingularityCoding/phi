from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from phi.harness import (
    CompactionSettings,
    PromptBudgetAnchor,
    build_context,
    effective_input_limit,
    estimate_prompt_tokens,
    estimate_request_tokens,
    safe_prompt_limit,
    should_compact,
)
from phi.model import ModelInfo, ModelRequest
from phi.settings import Settings


def test_context_keeps_sections_separate_and_builds_the_exact_request_order() -> None:
    context = build_context(
        stable_instructions="stable",
        tool_specs=[{"type": "function", "function": {"name": "lookup"}}],
        conversation_messages=[{"role": "user", "content": "hello"}],
        dropped_summary="earlier facts",
    )

    assert context.messages == ({"role": "user", "content": "hello"},)
    assert context.to_request(model="model-a").messages == [
        {"role": "system", "content": "stable"},
        {
            "role": "system",
            "content": "Dropped conversation history summary:\nearlier facts",
        },
        {"role": "user", "content": "hello"},
    ]
    assert context.character_counts == {
        "system_prompt": len("stable"),
        "dropped_summary": len("earlier facts"),
        "messages": len(json.dumps(context.messages, ensure_ascii=False, separators=(",", ":"))),
        "tools": len(json.dumps(context.tools, ensure_ascii=False, separators=(",", ":"))),
    }


def test_prompt_estimate_counts_the_complete_ascii_and_non_ascii_request() -> None:
    request = ModelRequest(
        messages=[
            {"role": "system", "content": "A"},
            {"role": "user", "content": "你好"},
        ],
        tools=[{"type": "function", "function": {"name": "x"}}],
        model="model-a",
    )

    assert estimate_request_tokens(request) == 67


def test_provider_usage_anchor_is_snapshotted_and_applied_only_to_a_matching_prefix() -> None:
    previous = ModelRequest(
        messages=[
            {"role": "system", "content": "A"},
            {"role": "user", "content": "你好"},
        ],
        tools=[{"type": "function", "function": {"name": "x"}}],
        model="model-a",
    )
    anchor = PromptBudgetAnchor(
        model_id="model-a",
        request=previous,
        local_estimate=67,
        prompt_tokens=80,
    )
    previous.messages[0]["content"] = "mutated"
    previous.tools[0]["function"]["name"] = "mutated"
    candidate = ModelRequest(
        messages=[
            {"role": "system", "content": "A"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "ok"},
        ],
        tools=[{"type": "function", "function": {"name": "x"}}],
        model="model-a",
    )

    estimate = estimate_prompt_tokens(candidate, model_id="model-a", anchor=anchor)

    assert anchor.request.messages[0]["content"] == "A"
    assert anchor.request.tools[0]["function"]["name"] == "x"
    assert estimate.tokens == 93
    assert estimate.used_provider_anchor is True
    assert estimate_prompt_tokens(
        candidate, model_id="model-b", anchor=anchor
    ).tokens == estimate_request_tokens(candidate)


def test_input_limit_policy_is_conservative_and_threshold_is_strictly_greater() -> None:
    policy = CompactionSettings(reserve_tokens=20, max_input_tokens=80)

    effective = effective_input_limit(ModelInfo("model-a", max_input_tokens=100), policy)

    assert effective == 80
    assert safe_prompt_limit(effective, policy) == 60
    assert should_compact(60, 60, policy) is False
    assert should_compact(61, 60, policy) is True


def test_compaction_settings_defaults_and_validation_are_environment_backed() -> None:
    settings = Settings()

    assert settings.compaction == CompactionSettings()
    assert settings.session_dir.as_posix().endswith("/.phi/sessions")
    with pytest.raises(ValidationError):
        Settings(compaction_reserve_tokens=-1)
    with pytest.raises(ValidationError):
        Settings(compaction_summary_max_tokens=0)
    with pytest.raises(ValidationError):
        Settings(compaction_max_input_tokens=0)
