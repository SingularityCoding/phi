from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from mcp import types

from phi.mcp.types import McpResource
from phi.tools import ApprovalClass, Tool, ToolFailure, tool

type RemoteToolCall = Callable[[dict[str, Any]], Awaitable[types.CallToolResult]]


class ResourceRuntime(Protocol):
    """Narrow runtime surface needed by the read-only Resource meta-tools."""

    @property
    def server_ids(self) -> tuple[str, ...]: ...

    @property
    def resources(self) -> tuple[McpResource, ...]: ...

    async def read_resource(self, server_id: str, uri: str) -> dict[str, Any] | ToolFailure: ...


def build_remote_tool(
    server_id: str,
    remote_tool: types.Tool,
    call_remote: RemoteToolCall,
    secrets: tuple[str, ...],
) -> Tool:
    """Adapt one untrusted remote MCP Tool to Phi's common Tool boundary."""

    tool_name = f"mcp__{server_id}__{remote_tool.name}"

    async def call(**arguments: Any) -> dict[str, Any] | ToolFailure:
        try:
            result = await call_remote(arguments)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            summary = safe_error_summary(error, secrets)
            return ToolFailure(f"{tool_name}: {summary}")
        envelope = _tool_result_envelope(result, secrets)
        if result.isError:
            serialized = json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return ToolFailure(f"{tool_name}: server_error: {serialized}")
        return envelope

    return Tool(
        name=tool_name,
        description=cast(
            str,
            redact_mcp_data(
                (remote_tool.description or "").strip()
                or f"Call {remote_tool.name} on MCP server {server_id}.",
                secrets,
            ),
        ),
        handler=call,
        args_schema=remote_tool.inputSchema,
        args_model=None,
        approval_class=ApprovalClass.UNCONFINED,
    )


def build_resource_tools(runtime: ResourceRuntime) -> tuple[Tool, ...]:
    """Build Resource meta-tools only when concrete Resources were discovered."""

    if not runtime.resources:
        return ()

    @tool(
        name="mcp_list_resources",
        description="List cached concrete Resources advertised by connected MCP servers.",
    )
    async def list_resources(server_id: str | None = None) -> list[dict[str, Any]] | ToolFailure:
        if server_id is not None and server_id not in runtime.server_ids:
            return ToolFailure(f"mcp_resource_error: unknown server {server_id!r}")
        return [
            {
                "server_id": resource.server_id,
                "uri": resource.uri,
                "name": resource.name,
                "description": resource.description,
                "mime_type": resource.mime_type,
            }
            for resource in runtime.resources
            if server_id is None or resource.server_id == server_id
        ]

    @tool(
        name="mcp_read_resource",
        description="Read one Resource from an explicit connected MCP server and URI.",
    )
    async def read_resource(server_id: str, uri: str) -> dict[str, Any] | ToolFailure:
        return await runtime.read_resource(server_id, uri)

    return list_resources, read_resource


def redact_mcp_data(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively redact configured environment values from MCP-originated data."""

    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            _redact_text(str(key), secrets): redact_mcp_data(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_mcp_data(item, secrets) for item in value]
    return value


def safe_error_summary(error: BaseException, secrets: tuple[str, ...]) -> str:
    """Build a bounded, redacted summary suitable for Events and Tool failures."""

    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return safe_error_summary(error.exceptions[0], secrets)
    if isinstance(error, FileNotFoundError):
        return "FileNotFoundError: executable not found"
    message = " ".join(_redact_text(str(error), secrets).split())
    if not message:
        return type(error).__name__
    return f"{type(error).__name__}: {message}"[:500]


def _tool_result_envelope(
    result: types.CallToolResult,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "content": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in result.content
        ]
    }
    if result.structuredContent is not None:
        envelope["structuredContent"] = result.structuredContent
    redacted = redact_mcp_data(envelope, secrets)
    if not isinstance(redacted, dict):
        raise TypeError("MCP Tool result envelope must remain an object")
    return redacted


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value
