from __future__ import annotations

import asyncio
import threading

import pytest

from phi.model import ToolCall, ToolResult
from phi.tools import (
    BYPASS_MODE,
    ApprovalClass,
    RuleBasedApprovalPolicy,
    Tool,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


async def test_dispatch_preserves_call_id_and_serializes_success() -> None:
    @tool(name="echo", description="Echo text.", approval_class=ApprovalClass.READ_ONLY)
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    dispatcher = ToolDispatcher(
        ToolRegistry([echo]),
        RuleBasedApprovalPolicy(BYPASS_MODE),
    )

    result = await dispatcher.dispatch(
        ToolCall(id="call-17", name="echo", arguments={"text": "hi"})
    )

    assert result == ToolResult(call_id="call-17", output='{"echo": "hi"}')


async def test_dispatch_returns_unknown_tool_as_a_result() -> None:
    dispatcher = ToolDispatcher(ToolRegistry(), RuleBasedApprovalPolicy(BYPASS_MODE))

    result = await dispatcher.dispatch(ToolCall(id="missing-1", name="missing", arguments={}))

    assert result.call_id == "missing-1"
    assert result.output == ""
    assert result.error == "unknown_tool: missing"


async def test_dispatch_rejects_malformed_local_arguments() -> None:
    @tool(name="bounded", description="Accept a count.")
    def bounded(count: int) -> str:
        return str(count)

    dispatcher = ToolDispatcher(
        ToolRegistry([bounded]),
        RuleBasedApprovalPolicy(BYPASS_MODE),
    )

    wrong_type = await dispatcher.dispatch(
        ToolCall(id="wrong-type", name="bounded", arguments={"count": "3"})
    )
    extra = await dispatcher.dispatch(
        ToolCall(id="extra", name="bounded", arguments={"count": 3, "other": True})
    )

    assert wrong_type.error is not None and wrong_type.error.startswith("invalid_arguments:")
    assert extra.error is not None and extra.error.startswith("invalid_arguments:")


async def test_sync_handler_does_not_block_other_async_work() -> None:
    started = threading.Event()
    release = threading.Event()

    @tool(name="sync", description="Wait synchronously.")
    def sync_handler() -> str:
        started.set()
        release.wait(timeout=1)
        return "finished"

    dispatcher = ToolDispatcher(
        ToolRegistry([sync_handler]),
        RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    dispatch_task = asyncio.create_task(
        dispatcher.dispatch(ToolCall(id="sync", name="sync", arguments={}))
    )

    await asyncio.to_thread(started.wait, 1)
    async_progress_observed = not dispatch_task.done()
    release.set()
    result = await dispatch_task

    assert async_progress_observed
    assert result.output == "finished"


async def test_handler_exception_is_returned_as_a_tool_result() -> None:
    @tool(name="broken", description="Raise an expected handler failure.")
    async def broken() -> str:
        raise LookupError("cannot complete")

    dispatcher = ToolDispatcher(
        ToolRegistry([broken]),
        RuleBasedApprovalPolicy(BYPASS_MODE),
    )

    result = await dispatcher.dispatch(ToolCall(id="broken", name="broken", arguments={}))

    assert result.error == "handler_error: LookupError: cannot complete"


@pytest.mark.parametrize("tool_timeout", [None, 0.01])
async def test_default_and_tool_specific_timeouts_return_tool_results(
    tool_timeout: float | None,
) -> None:
    never = asyncio.Event()

    @tool(name="wait", description="Wait forever.", timeout_seconds=tool_timeout)
    async def wait_forever() -> str:
        await never.wait()
        return "unreachable"

    default_timeout = 0.01 if tool_timeout is None else 1
    dispatcher = ToolDispatcher(
        ToolRegistry([wait_forever]),
        RuleBasedApprovalPolicy(BYPASS_MODE),
        default_timeout_seconds=default_timeout,
    )

    result = await dispatcher.dispatch(ToolCall(id="timeout", name="wait", arguments={}))

    assert result.error == "tool_timeout: exceeded 0.01 seconds"


async def test_dispatch_cancellation_propagates() -> None:
    started = asyncio.Event()
    never = asyncio.Event()

    @tool(name="cancel", description="Wait to be cancelled.")
    async def wait_for_cancellation() -> str:
        started.set()
        await never.wait()
        return "unreachable"

    dispatcher = ToolDispatcher(
        ToolRegistry([wait_for_cancellation]),
        RuleBasedApprovalPolicy(BYPASS_MODE),
    )
    task = asyncio.create_task(
        dispatcher.dispatch(ToolCall(id="cancel", name="cancel", arguments={}))
    )
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_async_callable_object_is_awaited() -> None:
    class AsyncCallable:
        async def __call__(self, text: str) -> str:
            return text.upper()

    callable_tool = Tool(
        name="callable",
        description="Invoke an async callable object.",
        handler=AsyncCallable(),
        args_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    dispatcher = ToolDispatcher(
        ToolRegistry([callable_tool]),
        RuleBasedApprovalPolicy(BYPASS_MODE),
    )

    result = await dispatcher.dispatch(
        ToolCall(id="callable", name="callable", arguments={"text": "await me"})
    )

    assert result == ToolResult(call_id="callable", output="AWAIT ME")


@pytest.mark.parametrize("timeout", [float("inf"), float("nan")])
def test_non_finite_configured_timeouts_are_rejected(timeout: float) -> None:
    def invalid_timeout() -> str:
        return "unreachable"

    with pytest.raises(ValueError, match="finite"):
        ToolDispatcher(
            ToolRegistry(),
            RuleBasedApprovalPolicy(BYPASS_MODE),
            default_timeout_seconds=timeout,
        )

    with pytest.raises(ValueError, match="finite"):
        tool(name="invalid-timeout", description="Invalid.", timeout_seconds=timeout)(
            invalid_timeout
        )
