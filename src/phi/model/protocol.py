from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from phi.model.events import ModelEvent
from phi.model.types import ModelRequest, ModelResponse


@runtime_checkable
class Model(Protocol):
    """A stateless boundary for one normalized Model interaction."""

    async def request(self, request: ModelRequest) -> ModelResponse: ...

    def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
