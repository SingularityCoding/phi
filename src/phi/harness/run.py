from __future__ import annotations

import asyncio
import inspect
import time
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from phi.harness.events import (
    ApprovalDecided,
    EventBus,
    EventEmitter,
    ModelCallCompleted,
    ModelCallDelta,
    ModelCallStarted,
    RunEvent,
    RunFinished,
    RunStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from phi.harness.hooks import CompletionDecision, Hooks, RunDecision
from phi.harness.snapshots import freeze_request, freeze_response
from phi.model import (
    Model,
    ModelRequest,
    ModelResponse,
    ResponseAssembler,
    ToolCall,
    ToolResult,
    serialize_assistant_response,
    serialize_tool_result,
)
from phi.tools import ApprovalDecision, ToolDispatcher


class RunStatus(StrEnum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Step:
    index: int
    request: ModelRequest
    response: ModelResponse
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    steps: tuple[Step, ...]
    output: str | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        if self.status is RunStatus.COMPLETED:
            if not isinstance(self.output, str):
                raise ValueError("completed Runs require a string output")
            if self.error is not None:
                raise ValueError("completed Runs cannot contain an error")
        elif self.output is not None:
            raise ValueError("non-completed Runs cannot contain an output")
        if self.status is RunStatus.FAILED:
            if self.error is None:
                raise ValueError("failed Runs require an error")
        elif self.error is not None:
            raise ValueError("only failed Runs can contain an error")


class _EventEmitter:
    def __init__(self, bus: EventEmitter[RunEvent], run_id: str) -> None:
        self.bus = bus
        self.run_id = run_id
        self._next_index = 0

    def next_index(self) -> int:
        index = self._next_index
        self._next_index += 1
        return index

    async def emit(self, event: RunEvent) -> None:
        await self.bus.emit(event)


async def run(
    initial_request: ModelRequest,
    model: Model,
    dispatcher: ToolDispatcher,
    *,
    max_steps: int,
    hooks: Hooks | None = None,
    event_bus: EventEmitter[RunEvent] | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Execute one bounded, streaming Model Run."""

    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")

    active_run_id = run_id if run_id is not None else str(uuid4())
    emitter = _EventEmitter(event_bus or EventBus[RunEvent](), active_run_id)
    working_messages = deepcopy(initial_request.messages)
    working_tools = deepcopy(initial_request.tools)
    active_hooks = hooks or Hooks()
    steps: list[Step] = []

    await emitter.emit(RunStarted(active_run_id, emitter.next_index()))

    for step_index in range(max_steps):
        if active_hooks.inject_messages is not None:
            try:
                injected_messages = await active_hooks.inject_messages()
                if not isinstance(injected_messages, list) or not all(
                    isinstance(message, str) for message in injected_messages
                ):
                    raise TypeError("inject_messages must return a list of strings")
            except Exception as error:
                return await _finish(
                    emitter,
                    RunResult(RunStatus.FAILED, tuple(steps), error=error),
                )
            working_messages.extend(
                {"role": "user", "content": message} for message in injected_messages
            )

        request = ModelRequest(
            messages=deepcopy(working_messages),
            tools=deepcopy(working_tools),
            model=initial_request.model,
            temperature=initial_request.temperature,
            max_tokens=initial_request.max_tokens,
        )
        request_snapshot = freeze_request(request)
        await emitter.emit(
            ModelCallStarted(
                active_run_id,
                emitter.next_index(),
                step_index,
                request_snapshot,
            )
        )
        assembler = ResponseAssembler()
        started_at = time.monotonic()
        try:
            stream = model.request_stream(request)
            try:
                async for delta in stream:
                    assembler.absorb(delta)
                    await emitter.emit(
                        ModelCallDelta(
                            active_run_id,
                            emitter.next_index(),
                            step_index,
                            delta,
                        )
                    )
            except asyncio.CancelledError:
                try:
                    await _close_stream(stream)
                except BaseException:
                    pass
                raise
            except BaseException:
                try:
                    await _close_stream(stream)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                raise
            else:
                await _close_stream(stream)
            response = assembler.build()
            model_latency = max(0.0, time.monotonic() - started_at)
        except Exception as error:
            return await _finish(
                emitter,
                RunResult(RunStatus.FAILED, tuple(steps), error=error),
            )

        response_snapshot = freeze_response(response)
        await emitter.emit(
            ModelCallCompleted(
                active_run_id,
                emitter.next_index(),
                step_index,
                response_snapshot,
                model_latency,
            )
        )
        if response.tool_calls:
            tool_results: list[ToolResult] = []
            for call in response.tool_calls:
                await emitter.emit(
                    ToolCallStarted(
                        active_run_id,
                        emitter.next_index(),
                        step_index,
                        call,
                    )
                )

                async def observe_approval(
                    observed_call: ToolCall,
                    decision: ApprovalDecision,
                    mode: str | None,
                    observed_step_index: int = step_index,
                ) -> None:
                    await emitter.emit(
                        ApprovalDecided(
                            active_run_id,
                            emitter.next_index(),
                            observed_step_index,
                            observed_call,
                            decision,
                            mode,
                        )
                    )

                tool_started_at = time.monotonic()
                try:
                    result = await dispatcher.dispatch(
                        deepcopy(call),
                        approval_policy=active_hooks.before_tool_call,
                        approval_observer=observe_approval,
                    )
                except Exception as error:
                    steps.append(
                        Step(
                            step_index,
                            request_snapshot,
                            response_snapshot,
                            tuple(tool_results),
                        )
                    )
                    return await _finish(
                        emitter,
                        RunResult(RunStatus.FAILED, tuple(steps), error=error),
                    )
                tool_latency = max(0.0, time.monotonic() - tool_started_at)
                tool_results.append(result)
                await emitter.emit(
                    ToolCallCompleted(
                        active_run_id,
                        emitter.next_index(),
                        step_index,
                        call,
                        result,
                        tool_latency,
                    )
                )

            steps.append(
                Step(
                    step_index,
                    request_snapshot,
                    response_snapshot,
                    tuple(tool_results),
                )
            )
            working_messages.append(serialize_assistant_response(response))
            working_messages.extend(serialize_tool_result(result) for result in tool_results)
            if step_index + 1 == max_steps:
                return await _finish(
                    emitter,
                    RunResult(RunStatus.MAX_STEPS, tuple(steps)),
                )
            continue

        steps.append(Step(step_index, request_snapshot, response_snapshot))
        output = response.content if response.content is not None else ""
        provisional_result = RunResult(
            RunStatus.COMPLETED,
            tuple(steps),
            output=output,
        )
        if active_hooks.before_run_complete is None:
            return await _finish(emitter, provisional_result)

        try:
            decision = await active_hooks.before_run_complete(_snapshot_result(provisional_result))
            if not isinstance(decision, CompletionDecision):
                raise TypeError("before_run_complete must return CompletionDecision")
        except Exception as error:
            return await _finish(
                emitter,
                RunResult(RunStatus.FAILED, tuple(steps), error=error),
            )

        if decision.decision is RunDecision.ACCEPT:
            return await _finish(emitter, provisional_result)

        working_messages.append(serialize_assistant_response(response))
        working_messages.append({"role": "user", "content": decision.feedback})
        if step_index + 1 == max_steps:
            return await _finish(
                emitter,
                RunResult(RunStatus.MAX_STEPS, tuple(steps)),
            )

    raise AssertionError("positive max_steps must enter the Run loop")


async def _finish(emitter: _EventEmitter, result: RunResult) -> RunResult:
    await emitter.emit(
        RunFinished(
            emitter.run_id,
            emitter.next_index(),
            _snapshot_result(result),
        )
    )
    return result


def _snapshot_result(result: RunResult) -> RunResult:
    return RunResult(
        status=result.status,
        steps=result.steps,
        output=result.output,
        error=result.error,
    )


async def _close_stream(stream: Any) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    outcome = close()
    if inspect.isawaitable(outcome):
        await outcome
