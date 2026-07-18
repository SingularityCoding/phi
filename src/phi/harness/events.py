from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

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


@dataclass(frozen=True)
class RunStarted:
    run_id: str
    event_index: int


@dataclass(frozen=True)
class ModelCallStarted:
    run_id: str
    event_index: int
    step_index: int
    request: ModelRequest

    def __post_init__(self) -> None:
        object.__setattr__(self, "request", freeze_request(self.request))


@dataclass(frozen=True)
class ModelCallDelta:
    run_id: str
    event_index: int
    step_index: int
    delta: ModelEvent

    def __post_init__(self) -> None:
        object.__setattr__(self, "delta", freeze_model_event(self.delta))


@dataclass(frozen=True)
class ModelCallCompleted:
    run_id: str
    event_index: int
    step_index: int
    response: ModelResponse
    latency_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", freeze_response(self.response))


@dataclass(frozen=True)
class ToolCallStarted:
    run_id: str
    event_index: int
    step_index: int
    call: ToolCall

    def __post_init__(self) -> None:
        object.__setattr__(self, "call", freeze_tool_call(self.call))


@dataclass(frozen=True)
class ToolCallCompleted:
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
class ApprovalDecided:
    run_id: str
    event_index: int
    step_index: int
    call: ToolCall
    decision: ApprovalDecision
    mode: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "call", freeze_tool_call(self.call))


@dataclass(frozen=True)
class RunFinished:
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
type EventListener = Callable[[RunEvent], Awaitable[object] | object]


class EventBus:
    """Deliver immutable Run Events to listeners in subscription order."""

    def __init__(self, listeners: Iterable[EventListener] = ()) -> None:
        self._listeners = list(listeners)

    def subscribe(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    async def emit(self, event: RunEvent) -> None:
        for listener in self._listeners:
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
