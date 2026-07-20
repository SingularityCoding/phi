"""在唯一可信边界完成 Tool Call 的校验、审批、执行与结果归一化。"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from phi.model import ToolCall, ToolResult
from phi.tools.approval import ApprovalDecision, ApprovalModeProvider, ApprovalPolicy
from phi.tools.registry import ToolRegistry
from phi.tools.types import Tool


@dataclass(frozen=True)
class ToolFailure:
    """应留在 Tool 往返内部的、handler 可预期失败。"""

    error: str


type ApprovalObserver = Callable[
    [ToolCall, ApprovalDecision, str | None],
    Awaitable[None] | None,
]


class ToolDispatcher:
    """唯一的异步参数校验、审批、超时和执行权限边界。"""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_policy: ApprovalPolicy,
        *,
        trusted_values: Mapping[str, object] | None = None,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        """绑定注册表、默认审批策略、可信注入值和外层超时。"""

        if not math.isfinite(default_timeout_seconds) or default_timeout_seconds <= 0:
            raise ValueError("dispatcher timeout must be finite and positive")
        self._registry = registry
        self._approval_policy = approval_policy
        self._trusted_values = dict(trusted_values or {})
        self._default_timeout_seconds = default_timeout_seconds

    async def dispatch(
        self,
        call: ToolCall,
        *,
        approval_policy: ApprovalPolicy | None = None,
        approval_observer: ApprovalObserver | None = None,
    ) -> ToolResult:
        """处理一个不可信 Tool Call，并把预期失败归一化为 Tool Result。"""

        # Tool 名称来自 Model 输出，查找失败是正常的 Tool Result，而不是 Run 失败。
        tool = self._registry.get(call.name)
        if tool is None:
            return ToolResult(call_id=call.id, output="", error=f"unknown_tool: {call.name}")

        try:
            # 本地 Tool 使用 Pydantic 严格校验；MCP Tool 则保留远端 schema 语义。
            arguments = self._validated_arguments(tool, call.arguments)
        except ValidationError as exc:
            details = json.dumps(exc.errors(include_url=False), ensure_ascii=False, default=str)
            return ToolResult(call_id=call.id, output="", error=f"invalid_arguments: {details}")

        policy = approval_policy if approval_policy is not None else self._approval_policy
        decision = await policy.decide(call, tool)
        # 观察者只记录已经作出的决定，不能修改审批行为。
        if approval_observer is not None:
            mode = policy.approval_mode_name if isinstance(policy, ApprovalModeProvider) else None
            observation = approval_observer(call, decision, mode)
            if inspect.isawaitable(observation):
                await observation
        if decision is ApprovalDecision.DENY:
            return ToolResult(call_id=call.id, output="", error=f"approval_denied: {tool.name}")

        # 可信值只能由运行时 wiring 注入，绝不能接受同名 Model 参数覆盖。
        for parameter in tool.injected_parameters:
            if parameter not in self._trusted_values:
                raise RuntimeError(f"missing trusted value for injected parameter {parameter!r}")
            arguments[parameter] = self._trusted_values[parameter]

        timeout = tool.timeout_seconds or self._default_timeout_seconds
        if tool.timeout_parameter is not None:
            requested_timeout = arguments.get(tool.timeout_parameter)
            if isinstance(requested_timeout, (int, float)):
                if not math.isfinite(requested_timeout) or requested_timeout <= 0:
                    return ToolResult(
                        call_id=call.id,
                        output="",
                        error="invalid_arguments: timeout must be finite and positive",
                    )
                # handler 内层操作可使用 Model 请求值；dispatcher 多留一秒负责取消清理。
                timeout = max(timeout, float(requested_timeout) + 1.0)
        try:
            output = await asyncio.wait_for(self._invoke(tool, arguments), timeout=timeout)
        except TimeoutError:
            return ToolResult(
                call_id=call.id,
                output="",
                error=f"tool_timeout: exceeded {timeout:g} seconds",
            )
        except asyncio.CancelledError:
            # Run 取消必须继续传播，不能被包装成 handler_error。
            raise
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                output="",
                error=f"handler_error: {type(exc).__name__}: {exc}",
            )

        if isinstance(output, ToolFailure):
            return ToolResult(call_id=call.id, output="", error=output.error)
        return ToolResult(call_id=call.id, output=_serialize_output(output))

    def with_registry(self, registry: ToolRegistry) -> ToolDispatcher:
        """保留策略与可信值，为受限 Tool 集合创建新的 dispatcher。"""

        return ToolDispatcher(
            registry,
            self._approval_policy,
            trusted_values=self._trusted_values,
            default_timeout_seconds=self._default_timeout_seconds,
        )

    def with_trusted_values(self, trusted_values: Mapping[str, object]) -> ToolDispatcher:
        """合并额外可信值，但不改变 dispatch 的调用边界。"""

        return ToolDispatcher(
            self._registry,
            self._approval_policy,
            trusted_values={**self._trusted_values, **dict(trusted_values)},
            default_timeout_seconds=self._default_timeout_seconds,
        )

    @staticmethod
    def _validated_arguments(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        """严格解析本地参数；无本地模型时把验证留给远端 Tool。"""

        if tool.args_model is None:
            return dict(arguments)
        validated = tool.args_model.model_validate(arguments)
        return {name: getattr(validated, name) for name in type(validated).model_fields}

    @staticmethod
    async def _invoke(tool: Tool, arguments: dict[str, Any]) -> Any:
        """统一调用异步、同步以及返回 awaitable 的同步 handler。"""

        if _is_async_callable(tool.handler):
            return await tool.handler(**arguments)
        # 同步函数进入工作线程，防止阻塞 Harness 所在的事件循环。
        output = await asyncio.to_thread(tool.handler, **arguments)
        # 某些可调用对象不是 coroutine function，却会在调用后返回 awaitable。
        if inspect.isawaitable(output):
            return await output
        return output


def _serialize_output(output: Any) -> str:
    """把常见 Python 返回值稳定地编码为 Model 可消费的文本。"""

    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json")
    elif is_dataclass(output) and not isinstance(output, type):
        output = asdict(output)
    try:
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(output)


def _is_async_callable(handler: Any) -> bool:
    """识别 async 函数以及实现 async ``__call__`` 的对象。"""

    return inspect.iscoroutinefunction(handler) or (
        callable(handler) and inspect.iscoroutinefunction(handler.__call__)
    )
