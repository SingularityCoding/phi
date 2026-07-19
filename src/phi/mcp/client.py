from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl, TypeAdapter

from phi.harness import EventBus, EventEmitter
from phi.mcp.config import McpConfig, McpServerConfig
from phi.mcp.tools import (
    build_remote_tool,
    build_resource_tools,
    redact_mcp_data,
    safe_error_summary,
)
from phi.mcp.types import (
    McpDiagnostic,
    McpEvent,
    McpPrompt,
    McpPromptArgument,
    McpPromptError,
    McpPromptMessage,
    McpPromptResult,
    McpResource,
    McpServerConnected,
    McpServerConnectFailed,
)
from phi.tools import ToolFailure, ToolRegistry

_URI_ADAPTER = TypeAdapter(AnyUrl)


class _Paginated(Protocol):
    @property
    def nextCursor(self) -> str | None: ...


@dataclass
class _ServerConnection:
    server_id: str
    session: ClientSession
    tools: tuple[types.Tool, ...]
    resources: tuple[types.Resource, ...]
    prompts: tuple[types.Prompt, ...]
    secrets: tuple[str, ...]
    owner_task: asyncio.Task[None]
    close_requested: asyncio.Event
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_requested.set()
        try:
            await asyncio.shield(self.owner_task)
        except asyncio.CancelledError:
            while not self.owner_task.done():
                try:
                    await asyncio.shield(self.owner_task)
                except asyncio.CancelledError:
                    continue
            raise


@dataclass(frozen=True)
class _ConnectedParts:
    session: ClientSession
    tools: tuple[types.Tool, ...]
    resources: tuple[types.Resource, ...]
    prompts: tuple[types.Prompt, ...]


class McpRuntime:
    """Connected MCP capabilities and their explicit async lifetime."""

    def __init__(
        self,
        connections: dict[str, _ServerConnection] | None = None,
        diagnostics: tuple[McpDiagnostic, ...] = (),
    ) -> None:
        self._connections = connections if connections is not None else {}
        self.diagnostics = diagnostics
        self._closed = False

    @property
    def server_ids(self) -> tuple[str, ...]:
        return tuple(self._connections)

    @property
    def server_tool_counts(self) -> tuple[tuple[str, int], ...]:
        """Return deterministic connected-server Tool counts for Host inspection."""

        return tuple(
            sorted(
                (connection.server_id, len(connection.tools))
                for connection in self._connections.values()
            )
        )

    @property
    def resources(self) -> tuple[McpResource, ...]:
        discovered = (
            McpResource(
                server_id=connection.server_id,
                uri=cast(str, redact_mcp_data(str(resource.uri), connection.secrets)),
                name=cast(str, redact_mcp_data(resource.name, connection.secrets)),
                description=cast(
                    str | None,
                    redact_mcp_data(resource.description, connection.secrets),
                ),
                mime_type=cast(
                    str | None,
                    redact_mcp_data(resource.mimeType, connection.secrets),
                ),
            )
            for connection in self._connections.values()
            for resource in connection.resources
        )
        return tuple(sorted(discovered, key=lambda item: (item.server_id, item.uri, item.name)))

    async def list_prompts(self, server_id: str | None = None) -> tuple[McpPrompt, ...]:
        """List cached Prompt metadata for trusted Host selection."""

        if server_id is not None and server_id not in self._connections:
            raise McpPromptError(f"unknown MCP server: {server_id}")
        prompts = (
            _prompt_metadata(connection.server_id, prompt, connection.secrets)
            for connection in self._connections.values()
            if server_id is None or connection.server_id == server_id
            for prompt in connection.prompts
        )
        return tuple(sorted(prompts, key=lambda item: (item.server_id, item.name)))

    async def get_prompt(
        self,
        command: str,
        arguments: Mapping[str, str],
    ) -> McpPromptResult:
        """Retrieve a Prompt through its exact user-visible command identity."""

        selected: tuple[_ServerConnection, types.Prompt] | None = None
        for connection in self._connections.values():
            for prompt in connection.prompts:
                if _prompt_command(connection.server_id, prompt.name) == command:
                    selected = connection, prompt
                    break
            if selected is not None:
                break
        if selected is None:
            raise McpPromptError(f"unknown MCP Prompt command: {command}")
        connection, prompt = selected
        try:
            result = await connection.session.get_prompt(prompt.name, dict(arguments))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            summary = safe_error_summary(error, connection.secrets)
            raise McpPromptError(f"MCP Prompt {command!r} failed: {summary}") from error
        return McpPromptResult(
            description=cast(
                str | None,
                redact_mcp_data(result.description, connection.secrets),
            ),
            messages=tuple(
                McpPromptMessage(
                    role=message.role,
                    content=cast(
                        Mapping[str, Any],
                        redact_mcp_data(
                            message.content.model_dump(
                                mode="json",
                                by_alias=True,
                                exclude_none=True,
                            ),
                            connection.secrets,
                        ),
                    ),
                )
                for message in result.messages
            ),
        )

    async def read_resource(self, server_id: str, uri: str) -> dict[str, Any] | ToolFailure:
        """Read one Resource from an explicit connected server."""

        connection = self._connections.get(server_id)
        if connection is None:
            return ToolFailure(f"mcp_resource_error: unknown server {server_id!r}")
        try:
            parsed_uri = _URI_ADAPTER.validate_python(uri)
            result = await connection.session.read_resource(parsed_uri)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            summary = safe_error_summary(error, connection.secrets)
            return ToolFailure(f"mcp_resource_error: {summary}")
        envelope = {
            "contents": [
                content.model_dump(mode="json", by_alias=True, exclude_none=True)
                for content in result.contents
            ]
        }
        return cast(dict[str, Any], redact_mcp_data(envelope, connection.secrets))

    async def close(self) -> None:
        """Close all client sessions and subprocesses exactly once."""

        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None
        for connection in reversed(tuple(self._connections.values())):
            try:
                await connection.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    async def __aenter__(self) -> McpRuntime:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()


async def connect_mcp_servers(
    config: McpConfig,
    *,
    cwd: Path,
    registry: ToolRegistry,
    events: EventEmitter[McpEvent] | None = None,
) -> McpRuntime:
    """Connect enabled stdio servers in stable order and isolate startup failures."""

    event_bus = events or EventBus[McpEvent]()
    connections: dict[str, _ServerConnection] = {}
    diagnostics: list[McpDiagnostic] = []
    runtime = McpRuntime(connections)
    try:
        for server_id in sorted(config.servers):
            server_config = config.servers[server_id]
            if not server_config.enabled:
                continue
            try:
                connection = await _connect_server(server_id, server_config, cwd)
                registered_tools = tuple(
                    build_remote_tool(
                        connection.server_id,
                        remote_tool,
                        _bind_remote_call(connection.session, remote_tool.name),
                        connection.secrets,
                    )
                    for remote_tool in connection.tools
                )
                registry.register_many(registered_tools)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if "connection" in locals():
                    await connection.close()
                    del connection
                summary = safe_error_summary(error, tuple(server_config.env.values()))
                diagnostics.append(McpDiagnostic(server_id, summary))
                await event_bus.emit(McpServerConnectFailed(server_id, summary))
                continue
            connections[server_id] = connection
            del connection
            await event_bus.emit(McpServerConnected(server_id, len(registered_tools)))
    except BaseException:
        await runtime.close()
        raise
    runtime.diagnostics = tuple(diagnostics)
    resource_tools = build_resource_tools(runtime)
    if resource_tools:
        try:
            registry.register_many(resource_tools)
        except BaseException:
            await runtime.close()
            raise
    return runtime


async def _connect_server(
    server_id: str,
    config: McpServerConfig,
    cwd: Path,
) -> _ServerConnection:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[_ConnectedParts] = loop.create_future()
    close_requested = asyncio.Event()
    owner_task = asyncio.create_task(
        _own_server(config, cwd, ready, close_requested),
        name=f"mcp:{server_id}",
    )
    try:
        parts = await asyncio.shield(ready)
    except asyncio.CancelledError:
        close_requested.set()
        if not ready.done():
            owner_task.cancel()
        try:
            await asyncio.shield(owner_task)
        except asyncio.CancelledError:
            pass
        raise
    except BaseException:
        close_requested.set()
        await owner_task
        raise
    return _ServerConnection(
        server_id=server_id,
        session=parts.session,
        tools=parts.tools,
        resources=parts.resources,
        prompts=parts.prompts,
        secrets=tuple(value for value in config.env.values() if value),
        owner_task=owner_task,
        close_requested=close_requested,
    )


async def _own_server(
    config: McpServerConfig,
    cwd: Path,
    ready: asyncio.Future[_ConnectedParts],
    close_requested: asyncio.Event,
) -> None:
    try:
        async with AsyncExitStack() as stack:
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            streams = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=config.command,
                        args=list(config.args),
                        env=dict(config.env),
                        cwd=cwd,
                    ),
                    errlog=errlog,
                )
            )
            session = await stack.enter_async_context(ClientSession(*streams))
            initialized = await session.initialize()
            tools = (
                await _list_all_tools(session) if initialized.capabilities.tools is not None else ()
            )
            resources = (
                await _list_all_resources(session)
                if initialized.capabilities.resources is not None
                else ()
            )
            prompts = (
                await _list_all_prompts(session)
                if initialized.capabilities.prompts is not None
                else ()
            )
            _validate_discovery_metadata(
                tools,
                resources,
                prompts,
                tuple(value for value in config.env.values() if value),
            )
            ready.set_result(
                _ConnectedParts(
                    session=session,
                    tools=tools,
                    resources=resources,
                    prompts=prompts,
                )
            )
            await close_requested.wait()
    except asyncio.CancelledError:
        if not ready.done():
            ready.cancel()
        raise
    except BaseException as error:
        if not ready.done():
            ready.set_exception(error)
            return
        raise


async def _list_all_tools(session: ClientSession) -> tuple[types.Tool, ...]:
    return await _list_all(session.list_tools, lambda result: result.tools)


async def _list_all_resources(session: ClientSession) -> tuple[types.Resource, ...]:
    return await _list_all(session.list_resources, lambda result: result.resources)


async def _list_all_prompts(session: ClientSession) -> tuple[types.Prompt, ...]:
    return await _list_all(session.list_prompts, lambda result: result.prompts)


async def _list_all[T, P: _Paginated](
    fetch_page: Callable[[str | None], Awaitable[P]],
    select_items: Callable[[P], list[T]],
) -> tuple[T, ...]:
    items: list[T] = []
    cursor: str | None = None
    while True:
        result = await fetch_page(cursor)
        items.extend(select_items(result))
        cursor = result.nextCursor
        if cursor is None:
            return tuple(items)


def _bind_remote_call(
    session: ClientSession,
    tool_name: str,
) -> Callable[[dict[str, Any]], Awaitable[types.CallToolResult]]:
    async def call(arguments: dict[str, Any]) -> types.CallToolResult:
        return await session.call_tool(tool_name, arguments)

    return call


def _validate_discovery_metadata(
    tools: tuple[types.Tool, ...],
    resources: tuple[types.Resource, ...],
    prompts: tuple[types.Prompt, ...],
    configured_values: tuple[str, ...],
) -> None:
    for remote_tool in tools:
        _reject_configured_value(remote_tool.name, configured_values, "Tool name")
        _reject_configured_value(remote_tool.inputSchema, configured_values, "Tool input schema")
    for resource in resources:
        _reject_configured_value(str(resource.uri), configured_values, "Resource URI")
        _reject_configured_value(resource.name, configured_values, "Resource name")
        _reject_configured_value(resource.mimeType, configured_values, "Resource MIME type")
    for prompt in prompts:
        _reject_configured_value(prompt.name, configured_values, "Prompt name")
        for argument in prompt.arguments or ():
            _reject_configured_value(
                argument.name,
                configured_values,
                "Prompt argument name",
            )


def _reject_configured_value(
    value: Any,
    configured_values: tuple[str, ...],
    location: str,
) -> None:
    if _contains_configured_value(value, configured_values):
        raise ValueError(f"server advertised a configured environment value in {location}")


def _contains_configured_value(value: Any, configured_values: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(configured in value for configured in configured_values)
    if isinstance(value, Mapping):
        return any(
            _contains_configured_value(key, configured_values)
            or _contains_configured_value(item, configured_values)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_configured_value(item, configured_values) for item in value)
    return False


def _prompt_metadata(
    server_id: str,
    prompt: types.Prompt,
    secrets: tuple[str, ...],
) -> McpPrompt:
    return McpPrompt(
        server_id=server_id,
        name=prompt.name,
        description=cast(str | None, redact_mcp_data(prompt.description, secrets)),
        command=_prompt_command(server_id, prompt.name),
        arguments=tuple(
            McpPromptArgument(
                name=argument.name,
                description=cast(
                    str | None,
                    redact_mcp_data(argument.description, secrets),
                ),
                required=bool(argument.required),
            )
            for argument in (prompt.arguments or ())
        ),
    )


def _prompt_command(server_id: str, prompt_name: str) -> str:
    return f"/mcp__{server_id}__{prompt_name}"
