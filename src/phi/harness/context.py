from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

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
