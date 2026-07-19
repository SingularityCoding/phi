from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import types
from mcp.server.fastmcp import FastMCP

pid_file = os.environ.get("PHI_MCP_PID_FILE")
if pid_file is not None:
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")


@asynccontextmanager
async def fixture_lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
    yield {}
    if os.environ.get("PHI_MCP_SLOW_SHUTDOWN") == "1":
        await asyncio.sleep(0.25)


server = FastMCP("phi-test-server", lifespan=fixture_lifespan)


@server.tool(description="Echo text and report deterministic runtime facts.")
async def echo(text: str) -> dict[str, object]:
    return {
        "cwd": Path.cwd().as_posix(),
        "environment_matches": os.environ.get("PHI_MCP_SECRET")
        == os.environ.get("PHI_MCP_EXPECTED_SECRET"),
        "text": text,
    }


@server.tool(description="Wait for a caller-supplied interval.")
async def wait(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "finished"


@server.tool(description="Terminate the stdio transport before returning a result.")
async def terminate() -> str:
    os._exit(17)


@server.tool(description="Return supported MCP content blocks and structured content.")
async def content_blocks() -> types.CallToolResult:
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text="plain text"),
            types.ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri="fixture://embedded",
                    mimeType="text/plain",
                    text="embedded text",
                ),
            ),
        ],
        structuredContent={"status": "ok"},
    )


@server.tool(description="Return a configured environment value for redaction tests.")
async def expose_secret(as_error: bool) -> types.CallToolResult:
    secret = os.environ.get("PHI_MCP_SECRET", "")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"configured={secret}")],
        structuredContent={"configured": secret},
        isError=as_error,
    )


if os.environ.get("PHI_MCP_ADVERTISE_LONG_TOOL_NAME") == "1":

    @server.tool(name="tool_name_that_is_intentionally_longer_than_the_wire_limit")
    async def overlength_tool() -> str:
        return "must never be registered"


advertised_secret = os.environ.get("PHI_MCP_ADVERTISE_SECRET_TOOL_NAME")
if advertised_secret is not None:

    @server.tool(name=f"expose_{advertised_secret}")
    async def secret_named_tool() -> str:
        return "must never be registered"


if os.environ.get("PHI_MCP_DISABLE_RESOURCES") != "1":

    @server.resource(
        "fixture://document",
        name="Fixture Document",
        description="A deterministic test Resource.",
        mime_type="text/plain",
    )
    async def document() -> str:
        return "Fixture Resource body."


@server.prompt(name="welcome", description="Build a deterministic greeting.")
async def welcome(name: str) -> str:
    return f"Welcome, {name}."


if __name__ == "__main__":
    if os.environ.get("PHI_MCP_HANG_AT_STARTUP") == "1":
        asyncio.run(asyncio.Event().wait())
    else:
        server.run(transport="stdio")
