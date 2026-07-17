from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from phi.model.errors import ModelProtocolError
from phi.model.events import (
    ContentDelta,
    FinishEvent,
    ModelEvent,
    ReasoningDelta,
    ToolCallDelta,
    UsageEvent,
)
from phi.model.types import ModelResponse, ToolCall


@dataclass
class _ToolCallBuffer:
    id: str | None = None
    name: str | None = None
    arguments: str = ""


class ResponseAssembler:
    """Incrementally assemble Model Events into one normalized response."""

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._content_observed = False
        self._reasoning_parts: list[str] = []
        self._reasoning_observed = False
        self._tool_calls: dict[int, _ToolCallBuffer] = {}
        self._usage = None
        self._finish_reason: str | None = None
        self._raw: dict[str, Any] = {}

    def absorb(self, event: ModelEvent) -> None:
        if isinstance(event, ContentDelta):
            self._content_observed = True
            self._content_parts.append(event.text)
        elif isinstance(event, ReasoningDelta):
            self._reasoning_observed = True
            self._reasoning_parts.append(event.text)
        elif isinstance(event, ToolCallDelta):
            buffer = self._tool_calls.setdefault(event.index, _ToolCallBuffer())
            if event.id is not None:
                buffer.id = event.id
            if event.name is not None:
                buffer.name = event.name
            buffer.arguments += event.arguments_fragment
        elif isinstance(event, FinishEvent):
            self._finish_reason = event.finish_reason
            self._raw = event.raw
        elif isinstance(event, UsageEvent):
            self._usage = event.usage
            self._raw = event.raw

    def build(self) -> ModelResponse:
        tool_calls = [
            self._build_tool_call(index, buffer)
            for index, buffer in sorted(self._tool_calls.items())
        ]
        return ModelResponse(
            content="".join(self._content_parts) if self._content_observed else None,
            reasoning="".join(self._reasoning_parts) if self._reasoning_observed else None,
            tool_calls=tool_calls,
            usage=self._usage,
            finish_reason=self._finish_reason,
            raw=self._raw,
        )

    @staticmethod
    def _build_tool_call(index: int, buffer: _ToolCallBuffer) -> ToolCall:
        if buffer.id is None or buffer.name is None:
            raise ModelProtocolError(f"Tool Call at index {index} is missing an id or name")

        try:
            arguments: Any = json.loads(
                buffer.arguments,
                parse_constant=_reject_non_json_constant,
            )
        except ValueError as exc:
            raise ModelProtocolError(
                f"Tool Call at index {index} has invalid JSON arguments"
            ) from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError(f"Tool Call at index {index} arguments must be a JSON object")

        return ToolCall(id=buffer.id, name=buffer.name, arguments=arguments)


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not a valid JSON constant")
