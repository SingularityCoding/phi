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
    """Expected handler failure that should remain inside the Tool round trip."""

    error: str


type ApprovalObserver = Callable[
    [ToolCall, ApprovalDecision, str | None],
    Awaitable[None] | None,
]


class ToolDispatcher:
    """The single async validation, approval, timeout, and execution boundary."""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_policy: ApprovalPolicy,
        *,
        trusted_values: Mapping[str, object] | None = None,
        default_timeout_seconds: float = 30.0,
    ) -> None:
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
        tool = self._registry.get(call.name)
        if tool is None:
            return ToolResult(call_id=call.id, output="", error=f"unknown_tool: {call.name}")

        try:
            arguments = self._validated_arguments(tool, call.arguments)
        except ValidationError as exc:
            details = json.dumps(exc.errors(include_url=False), ensure_ascii=False, default=str)
            return ToolResult(call_id=call.id, output="", error=f"invalid_arguments: {details}")

        policy = approval_policy if approval_policy is not None else self._approval_policy
        decision = await policy.decide(call, tool)
        if approval_observer is not None:
            mode = policy.approval_mode_name if isinstance(policy, ApprovalModeProvider) else None
            observation = approval_observer(call, decision, mode)
            if inspect.isawaitable(observation):
                await observation
        if decision is ApprovalDecision.DENY:
            return ToolResult(call_id=call.id, output="", error=f"approval_denied: {tool.name}")

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

    @staticmethod
    def _validated_arguments(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool.args_model is None:
            return dict(arguments)
        validated = tool.args_model.model_validate(arguments)
        return {name: getattr(validated, name) for name in type(validated).model_fields}

    @staticmethod
    async def _invoke(tool: Tool, arguments: dict[str, Any]) -> Any:
        if _is_async_callable(tool.handler):
            return await tool.handler(**arguments)
        output = await asyncio.to_thread(tool.handler, **arguments)
        if inspect.isawaitable(output):
            return await output
        return output


def _serialize_output(output: Any) -> str:
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
    return inspect.iscoroutinefunction(handler) or (
        callable(handler) and inspect.iscoroutinefunction(handler.__call__)
    )
