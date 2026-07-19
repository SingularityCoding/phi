from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from phi.harness import Event


@dataclass(frozen=True)
class McpServerConnected(Event):
    """An MCP server initialized and registered all of its Tools."""

    server_id: str
    tool_count: int


@dataclass(frozen=True)
class McpServerConnectFailed(Event):
    """One MCP server failed without blocking the remaining runtime."""

    server_id: str
    error: str


type McpEvent = McpServerConnected | McpServerConnectFailed


@dataclass(frozen=True)
class McpDiagnostic:
    """A safe startup diagnostic for one configured MCP server."""

    server_id: str
    reason: str

    def __str__(self) -> str:
        return f"MCP server {self.server_id!r}: {self.reason}"


@dataclass(frozen=True)
class McpResource:
    """Cached concrete Resource metadata from one connected MCP server."""

    server_id: str
    uri: str
    name: str
    description: str | None
    mime_type: str | None


@dataclass(frozen=True)
class McpPromptArgument:
    """One advertised argument for a user-selected MCP Prompt."""

    name: str
    description: str | None
    required: bool


@dataclass(frozen=True)
class McpPrompt:
    """Cached Prompt metadata with its stable user command identity."""

    server_id: str
    name: str
    description: str | None
    command: str
    arguments: tuple[McpPromptArgument, ...]


@dataclass(frozen=True)
class McpPromptMessage:
    """One normalized message returned by a trusted Prompt selection."""

    role: str
    content: Mapping[str, Any]


@dataclass(frozen=True)
class McpPromptResult:
    """Normalized MCP Prompt output for a trusted runtime caller."""

    description: str | None
    messages: tuple[McpPromptMessage, ...]


class McpPromptError(LookupError):
    """A trusted Prompt operation could not be completed."""
