from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from phi.harness.snapshots import (
    freeze_error,
    freeze_model_event,
    freeze_request,
    freeze_response,
    freeze_tool_call,
    freeze_tool_result,
)
from phi.model import ModelEvent, ModelRequest, ModelResponse, ToolCall, ToolResult
from phi.tools import ApprovalDecision

if TYPE_CHECKING:
    from phi.harness.run import RunResult


class Event:
    """Marker base for immutable notifications delivered through an EventBus."""


@dataclass(frozen=True)
class RunStarted(Event):
    run_id: str
    event_index: int


@dataclass(frozen=True)
class ModelCallStarted(Event):
    run_id: str
    event_index: int
    step_index: int
    request: ModelRequest

    def __post_init__(self) -> None:
        object.__setattr__(self, "request", freeze_request(self.request))


@dataclass(frozen=True)
class ModelCallDelta(Event):
    run_id: str
    event_index: int
    step_index: int
    delta: ModelEvent

    def __post_init__(self) -> None:
        object.__setattr__(self, "delta", freeze_model_event(self.delta))


@dataclass(frozen=True)
class ModelCallCompleted(Event):
    run_id: str
    event_index: int
    step_index: int
    response: ModelResponse
    latency_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", freeze_response(self.response))


@dataclass(frozen=True)
class ToolCallStarted(Event):
    run_id: str
    event_index: int
    step_index: int
    call: ToolCall

    def __post_init__(self) -> None:
        object.__setattr__(self, "call", freeze_tool_call(self.call))


@dataclass(frozen=True)
class ToolCallCompleted(Event):
    run_id: str
    event_index: int
    step_index: int
    call: ToolCall
    result: ToolResult
    latency_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "call", freeze_tool_call(self.call))
        object.__setattr__(self, "result", freeze_tool_result(self.result))


@dataclass(frozen=True)
class ApprovalDecided(Event):
    run_id: str
    event_index: int
    step_index: int
    call: ToolCall
    decision: ApprovalDecision
    mode: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "call", freeze_tool_call(self.call))


@dataclass(frozen=True)
class RunFinished(Event):
    run_id: str
    event_index: int
    result: RunResult

    def __post_init__(self) -> None:
        from phi.harness.run import RunResult, Step

        object.__setattr__(
            self,
            "result",
            RunResult(
                status=self.result.status,
                steps=tuple(
                    Step(
                        index=step.index,
                        request=freeze_request(step.request),
                        response=freeze_response(step.response),
                        tool_results=tuple(
                            freeze_tool_result(result) for result in step.tool_results
                        ),
                    )
                    for step in self.result.steps
                ),
                output=self.result.output,
                error=freeze_error(self.result.error),
            ),
        )


type RunEvent = (
    RunStarted
    | ModelCallStarted
    | ModelCallDelta
    | ModelCallCompleted
    | ToolCallStarted
    | ToolCallCompleted
    | ApprovalDecided
    | RunFinished
)
type EventListener[TEvent: Event] = Callable[[TEvent], Awaitable[object] | object]


class EventEmitter[TEvent: Event](Protocol):
    """Structural Event-delivery boundary for producers of one Event family."""

    async def emit(self, event: TEvent) -> None: ...


class EventBus[TEvent: Event]:
    """Deliver immutable Events to listeners in subscription order."""

    def __init__(self, listeners: Iterable[EventListener[TEvent]] = ()) -> None:
        self._listeners = list(listeners)

    def subscribe(self, listener: EventListener[TEvent]) -> None:
        self._listeners.append(listener)

    async def emit(self, event: TEvent) -> None:
        for listener in self._listeners:
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
