from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence

from phi.model.assembler import ResponseAssembler
from phi.model.events import (
    ContentDelta,
    FinishEvent,
    ModelEvent,
    ReasoningDelta,
    ToolCallDelta,
    UsageEvent,
)
from phi.model.types import ModelRequest, ModelResponse

type ScriptEntry = ModelResponse | Sequence[ModelEvent] | Exception


class ScriptedModel:
    """A deterministic offline Model that consumes a finite response script."""

    def __init__(self, script: Iterable[ScriptEntry]) -> None:
        self._script = deque(script)
        self.requests: list[ModelRequest] = []

    async def request(self, request: ModelRequest) -> ModelResponse:
        assembler = ResponseAssembler()
        async for event in self.request_stream(request):
            assembler.absorb(event)
        return assembler.build()

    async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self._script:
            raise RuntimeError("ScriptedModel response script exhausted")

        entry = self._script.popleft()
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, ModelResponse):
            for event in _events_from_response(entry):
                yield event
            return

        for event in entry:
            yield event


def _events_from_response(response: ModelResponse) -> list[ModelEvent]:
    events: list[ModelEvent] = []
    if response.reasoning is not None:
        events.append(ReasoningDelta(response.reasoning))
    if response.content is not None:
        events.append(ContentDelta(response.content))
    events.extend(
        ToolCallDelta(
            index=index,
            id=tool_call.id,
            name=tool_call.name,
            arguments_fragment=json.dumps(tool_call.arguments),
        )
        for index, tool_call in enumerate(response.tool_calls)
    )
    events.append(FinishEvent(response.finish_reason, response.raw))
    if response.usage is not None:
        events.append(UsageEvent(response.usage, response.raw))
    return events
