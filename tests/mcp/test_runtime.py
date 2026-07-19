from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from phi.harness import EventBus
from phi.mcp import (
    McpConfig,
    McpPrompt,
    McpPromptArgument,
    McpPromptError,
    McpPromptMessage,
    McpPromptResult,
    McpServerConfig,
    McpServerConnected,
    McpServerConnectFailed,
    connect_mcp_servers,
)
from phi.model import ToolCall
from phi.tools import (
    BYPASS_MODE,
    DEFAULT_MODE,
    PLAN_MODE,
    ApprovalClass,
    RuleBasedApprovalPolicy,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


async def test_stdio_server_connects_discovers_and_dispatches_its_tool(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    fixture = Path(__file__).with_name("stdio_fixture.py")
    secret = "configured-value-must-not-be-observed"
    config = McpConfig(
        mcpServers={
            "demo": McpServerConfig(
                command=sys.executable,
                args=(str(fixture),),
                env={
                    "PHI_MCP_SECRET": secret,
                    "PHI_MCP_EXPECTED_SECRET": secret,
                },
            )
        }
    )
    registry = ToolRegistry()
    events: list[object] = []

    runtime = await connect_mcp_servers(
        config,
        cwd=cwd,
        registry=registry,
        events=EventBus([events.append]),
    )
    try:
        assert runtime.server_ids == ("demo",)
        assert runtime.diagnostics == ()
        assert events == [McpServerConnected(server_id="demo", tool_count=5)]
        assert secret not in repr(runtime)
        assert secret not in repr(events)

        registered = registry.get("mcp__demo__echo")
        assert registered is not None
        assert registered.description == "Echo text and report deterministic runtime facts."
        assert registered.args_model is None
        assert registered.approval_class is ApprovalClass.UNCONFINED
        assert registered.args_schema == {
            "properties": {"text": {"title": "Text", "type": "string"}},
            "required": ("text",),
            "title": "echoArguments",
            "type": "object",
        }

        dispatcher = ToolDispatcher(
            registry,
            RuleBasedApprovalPolicy(BYPASS_MODE),
        )
        result = await dispatcher.dispatch(ToolCall("call-1", "mcp__demo__echo", {"text": "hello"}))

        assert result.call_id == "call-1"
        assert result.error is None
        envelope = json.loads(result.output)
        assert envelope["structuredContent"] == {
            "cwd": cwd.as_posix(),
            "environment_matches": True,
            "text": "hello",
        }
        assert envelope["content"][0]["type"] == "text"

        blocks = await dispatcher.dispatch(ToolCall("blocks-call", "mcp__demo__content_blocks", {}))
        assert blocks.error is None
        assert json.loads(blocks.output) == {
            "content": [
                {"text": "plain text", "type": "text"},
                {"data": "aW1hZ2U=", "mimeType": "image/png", "type": "image"},
                {
                    "resource": {
                        "mimeType": "text/plain",
                        "text": "embedded text",
                        "uri": "fixture://embedded",
                    },
                    "type": "resource",
                },
            ],
            "structuredContent": {"status": "ok"},
        }

        successful_leak = await dispatcher.dispatch(
            ToolCall(
                "secret-success",
                "mcp__demo__expose_secret",
                {"as_error": False},
            )
        )
        assert successful_leak.error is None
        assert secret not in successful_leak.output
        assert "[redacted]" in successful_leak.output

        error_leak = await dispatcher.dispatch(
            ToolCall(
                "secret-error",
                "mcp__demo__expose_secret",
                {"as_error": True},
            )
        )
        assert error_leak.output == ""
        assert error_leak.error is not None
        assert secret not in error_leak.error
        assert "[redacted]" in error_leak.error
    finally:
        await runtime.close()


async def test_failed_server_is_diagnosed_and_does_not_block_later_healthy_servers(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    fixture = str(Path(__file__).with_name("stdio_fixture.py"))
    secret = "never-emit-this-configured-value"
    config = McpConfig(
        mcpServers={
            "a-failed": McpServerConfig(
                command="phi-command-that-does-not-exist",
                env={"TOKEN": secret},
            ),
            "z-healthy": McpServerConfig(command=sys.executable, args=(fixture,)),
        }
    )
    registry = ToolRegistry()
    events: list[object] = []

    def faulty_listener(_event: object) -> None:
        raise RuntimeError("listener failure must be isolated")

    runtime = await connect_mcp_servers(
        config,
        cwd=cwd,
        registry=registry,
        events=EventBus([faulty_listener, events.append]),
    )
    try:
        assert runtime.server_ids == ("z-healthy",)
        assert len(runtime.diagnostics) == 1
        assert runtime.diagnostics[0].server_id == "a-failed"
        assert "executable not found" in runtime.diagnostics[0].reason
        assert [type(event) for event in events] == [
            McpServerConnectFailed,
            McpServerConnected,
        ]
        assert secret not in repr(runtime.diagnostics)
        assert secret not in repr(events)
        assert registry.get("mcp__z-healthy__echo") is not None
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    ("server_id", "preexisting_name"),
    [
        ("collision", "mcp__collision__echo"),
        ("illegal server id", "unrelated"),
    ],
)
async def test_invalid_or_colliding_generated_names_register_none_of_that_server(
    tmp_path: Path,
    server_id: str,
    preexisting_name: str,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    @tool(name=preexisting_name, description="Existing Tool keeps precedence.")
    async def existing() -> str:
        return "existing"

    registry = ToolRegistry([existing])
    events: list[object] = []
    runtime = await connect_mcp_servers(
        McpConfig(
            mcpServers={
                server_id: McpServerConfig(
                    command=sys.executable,
                    args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                )
            }
        ),
        cwd=cwd,
        registry=registry,
        events=EventBus([events.append]),
    )
    try:
        assert runtime.server_ids == ()
        assert registry.get(preexisting_name) is existing
        assert registry.get(f"mcp__{server_id}__wait") is None
        assert len(events) == 1 and isinstance(events[0], McpServerConnectFailed)
    finally:
        await runtime.close()


async def test_overlength_generated_name_isolates_the_entire_server(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    registry = ToolRegistry()
    events: list[object] = []

    runtime = await connect_mcp_servers(
        McpConfig(
            mcpServers={
                "long": McpServerConfig(
                    command=sys.executable,
                    args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                    env={"PHI_MCP_ADVERTISE_LONG_TOOL_NAME": "1"},
                )
            }
        ),
        cwd=cwd,
        registry=registry,
        events=EventBus([events.append]),
    )
    try:
        assert runtime.server_ids == ()
        assert registry.get("mcp__long__echo") is None
        assert len(events) == 1 and isinstance(events[0], McpServerConnectFailed)
        assert "64 characters" in events[0].error
    finally:
        await runtime.close()


async def test_configured_value_in_structural_metadata_isolates_server(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    secret = "secret_metadata_value"
    registry = ToolRegistry()
    events: list[object] = []

    runtime = await connect_mcp_servers(
        McpConfig(
            mcpServers={
                "metadata": McpServerConfig(
                    command=sys.executable,
                    args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                    env={
                        "PHI_MCP_SECRET": secret,
                        "PHI_MCP_ADVERTISE_SECRET_TOOL_NAME": secret,
                    },
                )
            }
        ),
        cwd=cwd,
        registry=registry,
        events=EventBus([events.append]),
    )
    try:
        assert runtime.server_ids == ()
        assert registry.get("mcp__metadata__echo") is None
        assert len(runtime.diagnostics) == 1
        assert len(events) == 1 and isinstance(events[0], McpServerConnectFailed)
        assert "configured environment value in Tool name" in events[0].error
        assert secret not in repr(runtime.diagnostics)
        assert secret not in repr(events)
    finally:
        await runtime.close()


async def test_mcp_tools_use_existing_approval_timeout_error_and_cancellation_boundaries(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    registry = ToolRegistry()
    runtime = await connect_mcp_servers(
        McpConfig(
            mcpServers={
                "policy": McpServerConfig(
                    command=sys.executable,
                    args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                )
            }
        ),
        cwd=cwd,
        registry=registry,
    )
    try:
        call = ToolCall("approval-call", "mcp__policy__echo", {"text": "hello"})
        for mode in (DEFAULT_MODE, PLAN_MODE):
            dispatcher = ToolDispatcher(registry, RuleBasedApprovalPolicy(mode))
            denied = await dispatcher.dispatch(call)
            assert denied.error == "approval_denied: mcp__policy__echo"

        dispatcher = ToolDispatcher(
            registry,
            RuleBasedApprovalPolicy(BYPASS_MODE),
            default_timeout_seconds=0.05,
        )
        rejected = await dispatcher.dispatch(ToolCall("invalid-call", "mcp__policy__echo", {}))
        assert rejected.call_id == "invalid-call"
        assert rejected.output == ""
        assert rejected.error is not None
        assert rejected.error.startswith("mcp__policy__echo: server_error:")

        timed_out = await dispatcher.dispatch(
            ToolCall("timeout-call", "mcp__policy__wait", {"seconds": 1.0})
        )
        assert timed_out.call_id == "timeout-call"
        assert timed_out.error == "tool_timeout: exceeded 0.05 seconds"

        task = asyncio.create_task(
            dispatcher.dispatch(ToolCall("cancel-call", "mcp__policy__wait", {"seconds": 1.0}))
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        recovered = await dispatcher.dispatch(
            ToolCall("recovered-call", "mcp__policy__echo", {"text": "recovered"})
        )
        assert recovered.error is None
    finally:
        await runtime.close()


async def test_prompts_are_available_only_through_trusted_namespaced_operations(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    config = McpConfig(
        mcpServers={
            "prompts": McpServerConfig(
                command=sys.executable,
                args=(str(Path(__file__).with_name("stdio_fixture.py")),),
            )
        }
    )
    registry = ToolRegistry()
    runtime = await connect_mcp_servers(config, cwd=cwd, registry=registry)
    try:
        assert await runtime.list_prompts() == (
            McpPrompt(
                server_id="prompts",
                name="welcome",
                description="Build a deterministic greeting.",
                command="/mcp__prompts__welcome",
                arguments=(McpPromptArgument(name="name", description=None, required=True),),
            ),
        )

        result = await runtime.get_prompt(
            "/mcp__prompts__welcome",
            {"name": "Ada"},
        )

        assert result == McpPromptResult(
            description="Build a deterministic greeting.",
            messages=(
                McpPromptMessage(
                    role="user",
                    content={"type": "text", "text": "Welcome, Ada."},
                ),
            ),
        )
        assert all("welcome" not in spec["function"]["name"] for spec in registry.specs())
        with pytest.raises(McpPromptError, match="unknown MCP Prompt"):
            await runtime.get_prompt("/mcp__prompts__missing", {})
    finally:
        await runtime.close()


async def test_concrete_resources_are_exposed_only_through_two_read_only_meta_tools(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    config = McpConfig(
        mcpServers={
            "resources": McpServerConfig(
                command=sys.executable,
                args=(str(Path(__file__).with_name("stdio_fixture.py")),),
            )
        }
    )
    registry = ToolRegistry()
    runtime = await connect_mcp_servers(config, cwd=cwd, registry=registry)
    try:
        list_tool = registry.get("mcp_list_resources")
        read_tool = registry.get("mcp_read_resource")
        assert list_tool is not None and read_tool is not None
        assert list_tool.approval_class is ApprovalClass.READ_ONLY
        assert read_tool.approval_class is ApprovalClass.READ_ONLY
        dispatcher = ToolDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE))

        listed = await dispatcher.dispatch(ToolCall("list-1", "mcp_list_resources", {}))
        assert listed.error is None
        assert json.loads(listed.output) == [
            {
                "description": "A deterministic test Resource.",
                "mime_type": "text/plain",
                "name": "Fixture Document",
                "server_id": "resources",
                "uri": "fixture://document",
            }
        ]

        read = await dispatcher.dispatch(
            ToolCall(
                "read-1",
                "mcp_read_resource",
                {"server_id": "resources", "uri": "fixture://document"},
            )
        )
        assert read.error is None
        assert json.loads(read.output) == {
            "contents": [
                {
                    "mimeType": "text/plain",
                    "text": "Fixture Resource body.",
                    "uri": "fixture://document",
                }
            ]
        }

        unknown = await dispatcher.dispatch(
            ToolCall(
                "read-missing",
                "mcp_read_resource",
                {"server_id": "missing", "uri": "fixture://document"},
            )
        )
        assert unknown.call_id == "read-missing"
        assert unknown.error == "mcp_resource_error: unknown server 'missing'"

        missing_uri = await dispatcher.dispatch(
            ToolCall(
                "read-unknown-uri",
                "mcp_read_resource",
                {"server_id": "resources", "uri": "fixture://missing"},
            )
        )
        assert missing_uri.error is not None
        assert missing_uri.error.startswith("mcp_resource_error:")
    finally:
        await runtime.close()


async def test_resource_meta_tools_are_absent_without_concrete_resources(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    registry = ToolRegistry()
    runtime = await connect_mcp_servers(
        McpConfig(
            mcpServers={
                "empty": McpServerConfig(
                    command=sys.executable,
                    args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                    env={"PHI_MCP_DISABLE_RESOURCES": "1"},
                )
            }
        ),
        cwd=cwd,
        registry=registry,
    )
    try:
        assert runtime.resources == ()
        assert registry.get("mcp_list_resources") is None
        assert registry.get("mcp_read_resource") is None
        assert registry.get("mcp__empty__echo") is not None
    finally:
        await runtime.close()


async def test_abrupt_transport_failure_becomes_a_typed_tool_result_error(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    registry = ToolRegistry()
    runtime = await connect_mcp_servers(
        McpConfig(
            mcpServers={
                "transport": McpServerConfig(
                    command=sys.executable,
                    args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                )
            }
        ),
        cwd=cwd,
        registry=registry,
    )
    try:
        dispatcher = ToolDispatcher(registry, RuleBasedApprovalPolicy(BYPASS_MODE))

        result = await dispatcher.dispatch(
            ToolCall("transport-call", "mcp__transport__terminate", {})
        )

        assert result.call_id == "transport-call"
        assert result.output == ""
        assert result.error is not None
        assert result.error.startswith("mcp__transport__terminate:")
    finally:
        await runtime.close()


async def test_runtime_close_is_cross_task_safe_idempotent_and_reaps_the_process(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    pid_path = tmp_path / "server.pid"
    config = McpConfig(
        mcpServers={
            "lifetime": McpServerConfig(
                command=sys.executable,
                args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                env={"PHI_MCP_PID_FILE": str(pid_path)},
            )
        }
    )

    runtime = await asyncio.create_task(
        connect_mcp_servers(config, cwd=cwd, registry=ToolRegistry())
    )
    pid = int(pid_path.read_text(encoding="utf-8"))

    await runtime.close()
    await runtime.close()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_startup_cancellation_cleans_already_connected_and_in_progress_servers(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    healthy_pid_path = tmp_path / "healthy.pid"
    hanging_pid_path = tmp_path / "hanging.pid"
    fixture = str(Path(__file__).with_name("stdio_fixture.py"))
    config = McpConfig(
        mcpServers={
            "a-healthy": McpServerConfig(
                command=sys.executable,
                args=(fixture,),
                env={"PHI_MCP_PID_FILE": str(healthy_pid_path)},
            ),
            "z-hanging": McpServerConfig(
                command=sys.executable,
                args=(fixture,),
                env={
                    "PHI_MCP_HANG_AT_STARTUP": "1",
                    "PHI_MCP_PID_FILE": str(hanging_pid_path),
                },
            ),
        }
    )
    startup = asyncio.create_task(connect_mcp_servers(config, cwd=cwd, registry=ToolRegistry()))
    async with asyncio.timeout(5):
        while not hanging_pid_path.exists():
            await asyncio.sleep(0.01)

    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    for pid_path in (healthy_pid_path, hanging_pid_path):
        pid = int(pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


async def test_shutdown_cancellation_propagates_only_after_process_cleanup(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    pid_path = tmp_path / "slow-shutdown.pid"
    runtime = await connect_mcp_servers(
        McpConfig(
            mcpServers={
                "slow": McpServerConfig(
                    command=sys.executable,
                    args=(str(Path(__file__).with_name("stdio_fixture.py")),),
                    env={
                        "PHI_MCP_PID_FILE": str(pid_path),
                        "PHI_MCP_SLOW_SHUTDOWN": "1",
                    },
                )
            }
        ),
        cwd=cwd,
        registry=ToolRegistry(),
    )
    pid = int(pid_path.read_text(encoding="utf-8"))

    shutdown = asyncio.create_task(runtime.close())
    await asyncio.sleep(0.02)
    shutdown.cancel()

    with pytest.raises(asyncio.CancelledError):
        await shutdown
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
