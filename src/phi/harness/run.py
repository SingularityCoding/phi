"""实现 Harness 拥有的有界、流式 Run 控制循环。"""

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
    """枚举 Run 可返回给调用方的终止状态。"""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Step:
    """记录一次 Model 请求/响应及该响应产生的 Tool Result。"""

    index: int
    request: ModelRequest
    response: ModelResponse
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class RunResult:
    """描述一个有界 Run 的终态、已完成 Step 与可选输出或错误。"""

    status: RunStatus
    steps: tuple[Step, ...]
    output: str | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        """验证 Run 状态与 output/error 字段的互斥不变量。"""

        # COMPLETED 是唯一携带文本输出的状态，即使最终文本是空字符串。
        if self.status is RunStatus.COMPLETED:
            if not isinstance(self.output, str):
                raise ValueError("completed Runs require a string output")
            if self.error is not None:
                raise ValueError("completed Runs cannot contain an error")
        elif self.output is not None:
            raise ValueError("non-completed Runs cannot contain an output")
        # FAILED 是唯一携带异常的状态；预期 Tool 失败应留在 Tool Result 中。
        if self.status is RunStatus.FAILED:
            if self.error is None:
                raise ValueError("failed Runs require an error")
        elif self.error is not None:
            raise ValueError("only failed Runs can contain an error")


class _EventEmitter:
    """为一个 Run 绑定 ID，并集中分配严格递增的 Event 序号。"""

    def __init__(self, bus: EventEmitter[RunEvent], run_id: str) -> None:
        """保存投递边界与 Run ID，并把首个 Event 序号设为零。"""

        self.bus = bus
        self.run_id = run_id
        self._next_index = 0

    def next_index(self) -> int:
        """返回当前 Event 序号，并为下一次投递递增计数器。"""

        index = self._next_index
        self._next_index += 1
        return index

    async def emit(self, event: RunEvent) -> None:
        """把已经带序号的 Run Event 交给底层投递边界。"""

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
    """执行一个有界且内部统一采用流式 Model 调用的 Run。

    Args:
        initial_request: 首个 Step 的消息、工具和 Model 参数模板。
        model: 每个 Step 调用的无状态 Model。
        dispatcher: Harness 用于处理 Tool Call 的唯一执行边界。
        max_steps: Run 允许的最大 Step 数，必须为正整数。
        hooks: 可选行为 Hook 集合。
        event_bus: 可选 Run Event 投递边界。
        run_id: 可选稳定 Run ID；省略时自动生成。

    Returns:
        描述完成、预算耗尽或失败终态的 RunResult。

    Raises:
        ValueError: max_steps 不是正整数。
        asyncio.CancelledError: Run 所在任务被取消；取消不会被转换为 FAILED。
    """

    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ValueError("max_steps must be a positive integer")

    # 以下可变值只属于本次 Run；Model 本身不保存对话历史或 Step 状态。
    active_run_id = run_id if run_id is not None else str(uuid4())
    emitter = _EventEmitter(event_bus or EventBus[RunEvent](), active_run_id)
    working_messages = deepcopy(initial_request.messages)
    working_tools = deepcopy(initial_request.tools)
    active_hooks = hooks or Hooks()
    steps: list[Step] = []

    await emitter.emit(RunStarted(active_run_id, emitter.next_index()))

    # max_steps 是总安全预算：Tool 往返和完成 Hook 重试都消耗同一份 Step 配额。
    for step_index in range(max_steps):
        if active_hooks.inject_messages is not None:
            # Steer 只在 Step 边界排空，不取消正在进行的 Model 请求。
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
            # 注入消息作为新的 User 输入追加，既不回写 initial_request，也不重启 Run。
            working_messages.extend(
                {"role": "user", "content": message} for message in injected_messages
            )

        # 每个 Step 都快照当前工作消息；后续工具结果只能影响下一个 Step。
        request = ModelRequest(
            messages=deepcopy(working_messages),
            tools=deepcopy(working_tools),
            model=initial_request.model,
            temperature=initial_request.temperature,
            max_tokens=initial_request.max_tokens,
        )
        request_snapshot = freeze_request(request)
        # Event 发出的是不可变请求快照，监听器不能修改即将送往 Model 的数据。
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
                    # 先吸收再通知：任意时刻的 Event 顺序都与最终组装顺序一致。
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
                # 任务取消必须先关闭异步生成器，再原样传播给外层服务。
                try:
                    await _close_stream(stream)
                except BaseException:
                    pass
                raise
            except BaseException:
                # 包括非 Exception 的异常也需触发流清理；清理失败不能遮蔽原始故障。
                try:
                    await _close_stream(stream)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                raise
            else:
                # 正常耗尽后仍显式关闭，释放实现可能持有的 HTTP/SSE 资源。
                await _close_stream(stream)
            # 只有流完整结束后才解析 Tool Call JSON，并形成归一化响应。
            response = assembler.build()
            model_latency = max(0.0, time.monotonic() - started_at)
        except Exception as error:
            # 非取消的 Model/协议/内部错误无法由当前循环安全恢复，Run 进入 FAILED。
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
            # v1 顺序处理同一响应内的 Tool Call，确保副作用与 Event 次序确定。
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
                    """把 dispatcher 的审批观察转换为当前 Step 的 Run Event。"""

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
                    # dispatcher 才能查找、审批、校验和执行；Model 仅有提议权。
                    result = await dispatcher.dispatch(
                        deepcopy(call),
                        approval_policy=active_hooks.before_tool_call,
                        approval_observer=observe_approval,
                    )
                except Exception as error:
                    # dispatcher 应把预期工具故障编码为 Tool Result；逸出异常表示内部缺陷。
                    # 已成功完成的结果仍被记录到当前 Step，保持 Trace 可解释性。
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

            # 完整记录当前 Step 后，把 Assistant Tool Call 与配对结果加入下一请求。
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
            # 工具往返需要下一 Step 才能得到最终答复；预算用尽时不能伪装成完成。
            if step_index + 1 == max_steps:
                return await _finish(
                    emitter,
                    RunResult(RunStatus.MAX_STEPS, tuple(steps)),
                )
            continue

        # 没有 Tool Call 表示 Model 提议最终文本；空 content 仍规范化为合法空输出。
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
            # Hook 只看到结果快照，不能通过修改嵌套请求或响应影响已完成 Step。
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

        # RETRY 将本次 Assistant 响应与纠正反馈追加到同一 Run，而非开启新 Run。
        working_messages.append(serialize_assistant_response(response))
        working_messages.append({"role": "user", "content": decision.feedback})
        if step_index + 1 == max_steps:
            return await _finish(
                emitter,
                RunResult(RunStatus.MAX_STEPS, tuple(steps)),
            )

    raise AssertionError("positive max_steps must enter the Run loop")


async def _finish(emitter: _EventEmitter, result: RunResult) -> RunResult:
    """投递唯一的 RunFinished 快照，然后返回调用方持有的原结果。"""

    await emitter.emit(
        RunFinished(
            emitter.run_id,
            emitter.next_index(),
            _snapshot_result(result),
        )
    )
    return result


def _snapshot_result(result: RunResult) -> RunResult:
    """重建 RunResult，使 dataclass 顶层与已冻结 Step 形成观察快照。"""

    return RunResult(
        status=result.status,
        steps=result.steps,
        output=result.output,
        error=result.error,
    )


async def _close_stream(stream: Any) -> None:
    """在对象支持时调用同步或异步 aclose，释放流资源。"""

    # 使用结构探测兼容实现 Model 协议但不暴露 aclose 的自定义迭代器。
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    outcome = close()
    if inspect.isawaitable(outcome):
        await outcome
