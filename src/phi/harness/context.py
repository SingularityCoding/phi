from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from phi.harness.compaction import PromptEstimate
from phi.harness.snapshots import freeze_request
from phi.model import ModelRequest


@dataclass(frozen=True)
class Context:
    """Finite, trusted projection used to construct one ordinary Model request."""

    system_prompt: str
    tools: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    dropped_summary: str | None = None

    def __post_init__(self) -> None:
        snapshot = freeze_request(
            ModelRequest(messages=list(self.messages), tools=list(self.tools))
        )
        object.__setattr__(self, "messages", tuple(snapshot.messages))
        object.__setattr__(self, "tools", tuple(snapshot.tools))

    def to_request(
        self,
        *,
        model: str | None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelRequest:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
        ]
        if self.dropped_summary is not None:
            messages.append(
                {
                    "role": "system",
                    "content": ("Dropped conversation history summary:\n" + self.dropped_summary),
                }
            )
        messages.extend(deepcopy(list(self.messages)))
        return ModelRequest(
            messages=messages,
            tools=deepcopy(list(self.tools)),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @property
    def character_counts(self) -> dict[str, int]:
        return {
            "system_prompt": len(self.system_prompt),
            "dropped_summary": len(self.dropped_summary or ""),
            "messages": len(json.dumps(self.messages, ensure_ascii=False, separators=(",", ":"))),
            "tools": len(json.dumps(self.tools, ensure_ascii=False, separators=(",", ":"))),
        }


@dataclass(frozen=True)
class ContextInspection:
    """Complete inspectable projection and budget diagnostics for one Context."""

    context: Context
    request: ModelRequest
    estimate: PromptEstimate
    effective_input_limit: int | None
    safe_prompt_limit: int | None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "request", freeze_request(self.request))

    @property
    def character_counts(self) -> Mapping[str, int]:
        return MappingProxyType(self.context.character_counts)


def build_context(
    *,
    stable_instructions: str,
    tool_specs: list[dict[str, Any]],
    conversation_messages: list[dict[str, Any]],
    dropped_summary: str | None,
) -> Context:
    return Context(
        system_prompt=stable_instructions,
        tools=tuple(deepcopy(tool_specs)),
        messages=tuple(deepcopy(conversation_messages)),
        dropped_summary=dropped_summary,
    )
