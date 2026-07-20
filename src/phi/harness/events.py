"""定义 Run 生命周期的不可变 Event 及其顺序投递机制。"""

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
    """标记通过 EventBus 投递的不可变通知。"""


@dataclass(frozen=True)
class RunStarted(Event):
    """表示 Harness 已开始一个 Run。"""

    run_id: str
    event_index: int


@dataclass(frozen=True)
class ModelCallStarted(Event):
    """表示一个 Step 的 Model 请求即将开始。"""

    run_id: str
    event_index: int
    step_index: int
    request: ModelRequest

    def __post_init__(self) -> None:
        """冻结请求，使 Event 监听器只能观察而不能影响 Run。"""

        object.__setattr__(self, "request", freeze_request(self.request))


@dataclass(frozen=True)
class ModelCallDelta(Event):
    """表示一个 Step 收到一个 Model 流式增量。"""

    run_id: str
    event_index: int
    step_index: int
    delta: ModelEvent

    def __post_init__(self) -> None:
        """冻结增量中可能携带的可变原始载荷。"""

        object.__setattr__(self, "delta", freeze_model_event(self.delta))


@dataclass(frozen=True)
class ModelCallCompleted(Event):
    """表示一个 Step 的 Model 请求已完成并组装。"""

    run_id: str
    event_index: int
    step_index: int
    response: ModelResponse
    latency_seconds: float

    def __post_init__(self) -> None:
        """冻结完整响应，隔离监听器与 Run 内部状态。"""

        object.__setattr__(self, "response", freeze_response(self.response))


@dataclass(frozen=True)
class ToolCallStarted(Event):
    """表示 Harness 开始处理 Model 提议的 Tool Call。"""

    run_id: str
    event_index: int
    step_index: int
    call: ToolCall

    def __post_init__(self) -> None:
        """冻结 Tool Call 参数。"""

        object.__setattr__(self, "call", freeze_tool_call(self.call))


@dataclass(frozen=True)
class ToolCallCompleted(Event):
    """表示 Harness 已为一个 Tool Call 产生 Tool Result。"""

    run_id: str
    event_index: int
    step_index: int
    call: ToolCall
    result: ToolResult
    latency_seconds: float

    def __post_init__(self) -> None:
        """冻结 Tool Call 与 Tool Result 的观察快照。"""

        object.__setattr__(self, "call", freeze_tool_call(self.call))
        object.__setattr__(self, "result", freeze_tool_result(self.result))


@dataclass(frozen=True)
class ApprovalDecided(Event):
    """表示 Approval Policy 已对 Tool Call 作出允许或拒绝决定。"""

    run_id: str
    event_index: int
    step_index: int
    call: ToolCall
    decision: ApprovalDecision
    mode: str | None

    def __post_init__(self) -> None:
        """冻结被审批的 Tool Call。"""

        object.__setattr__(self, "call", freeze_tool_call(self.call))


@dataclass(frozen=True)
class RunFinished(Event):
    """表示 Run 已产生终态结果。"""

    run_id: str
    event_index: int
    result: RunResult

    def __post_init__(self) -> None:
        """递归冻结结果中的 Step、响应、Tool Result 与错误。"""

        # 局部导入打破 events 与 run 类型之间的运行时循环依赖。
        from phi.harness.run import RunResult, Step

        # 重建整个结果而不是仅冻结顶层 dataclass，确保嵌套 wire 字典也不可变。
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
# 监听器返回值刻意被忽略：Event 是通知，不能成为改变 Harness 行为的 Hook。
type EventListener[TEvent: Event] = Callable[[TEvent], Awaitable[object] | object]


class EventEmitter[TEvent: Event](Protocol):
    """定义某一 Event 家族生产者依赖的结构化投递边界。"""

    async def emit(self, event: TEvent) -> None:
        """向已配置的观察者投递一个 Event。"""
        ...


class EventBus[TEvent: Event]:
    """按订阅顺序把不可变 Event 投递给监听器。"""

    def __init__(self, listeners: Iterable[EventListener[TEvent]] = ()) -> None:
        """复制初始监听器序列，建立独立的订阅列表。"""

        self._listeners = list(listeners)

    def subscribe(self, listener: EventListener[TEvent]) -> None:
        """把监听器追加到确定性的投递顺序末尾。"""

        self._listeners.append(listener)

    async def emit(self, event: TEvent) -> None:
        """顺序调用全部监听器，并隔离普通监听器失败。"""

        # 顺序 await 让 Trace 与测试观察到稳定次序；不创建失控的后台任务。
        for listener in self._listeners:
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            # 取消属于任务控制流，必须传播；普通观察故障则不能击穿活跃 Run。
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
