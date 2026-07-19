from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


@dataclass(frozen=True)
class McpConfigDiagnostic:
    """A safe, actionable diagnostic for one MCP configuration source."""

    source_path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.source_path}: {self.reason}"


class McpConfigError(ValueError):
    """A present MCP configuration source could not be loaded safely."""

    def __init__(self, diagnostic: McpConfigDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(str(diagnostic))


class McpConfigMutationError(ValueError):
    """A requested source-local MCP configuration mutation is invalid."""


class McpServerConfig(BaseModel):
    """One untrusted stdio MCP server definition."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def _command_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be blank")
        return value


class McpConfig(BaseModel):
    """One validated MCP configuration source."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")


@dataclass(frozen=True)
class ConfiguredMcpServer:
    server_id: str
    source: Literal["project", "global"]
    config: McpServerConfig


async def load_mcp_config(path: Path) -> McpConfig:
    """Load one MCP configuration source; a missing file is an empty source."""

    if not await asyncio.to_thread(path.exists):
        return McpConfig()
    try:
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise McpConfigError(
            McpConfigDiagnostic(
                path,
                f"cannot read MCP configuration ({type(error).__name__})",
            )
        ) from error
    try:
        return McpConfig.model_validate_json(content)
    except ValidationError as error:
        raise McpConfigError(McpConfigDiagnostic(path, _safe_validation_reason(error))) from error


async def load_merged_mcp_config(global_path: Path, project_path: Path) -> McpConfig:
    """Load and merge global and project sources with complete project replacement."""

    global_config = await load_mcp_config(global_path)
    project_config = await load_mcp_config(project_path)
    return McpConfig(mcpServers={**global_config.servers, **project_config.servers})


async def save_mcp_config(path: Path, config: McpConfig) -> None:
    """Atomically save one selected MCP source in deterministic JSON form."""

    try:
        await asyncio.to_thread(_save_mcp_config, path, config)
    except OSError as error:
        raise McpConfigError(
            McpConfigDiagnostic(
                path,
                f"cannot save MCP configuration ({type(error).__name__})",
            )
        ) from error


async def add_mcp_server(
    path: Path,
    server_id: str,
    command: str,
    args: tuple[str, ...] = (),
    *,
    source: Literal["project", "global"],
) -> None:
    """Validate and atomically add one stdio definition to one selected source."""

    _validate_cli_server_id(server_id)
    current = await load_mcp_config(path)
    if server_id in current.servers:
        raise McpConfigMutationError(
            f"MCP server {server_id!r} already exists in {source} MCP configuration"
        )
    updated = McpConfig(
        mcpServers={
            **current.servers,
            server_id: McpServerConfig(command=command, args=args),
        }
    )
    await save_mcp_config(path, updated)


async def remove_mcp_server(
    path: Path,
    server_id: str,
    *,
    source: Literal["project", "global"],
) -> None:
    """Atomically remove one definition from exactly one selected source."""

    current = await load_mcp_config(path)
    if server_id not in current.servers:
        raise McpConfigMutationError(
            f"MCP server {server_id!r} does not exist in {source} MCP configuration"
        )
    updated = McpConfig(
        mcpServers={
            configured_id: config
            for configured_id, config in current.servers.items()
            if configured_id != server_id
        }
    )
    await save_mcp_config(path, updated)


async def list_configured_mcp_servers(
    global_path: Path,
    project_path: Path,
    *,
    global_only: bool = False,
) -> tuple[ConfiguredMcpServer, ...]:
    """Return global-only or effective project-over-global definitions with provenance."""

    global_config = await load_mcp_config(global_path)
    if global_only:
        return tuple(
            ConfiguredMcpServer(server_id, "global", config)
            for server_id, config in sorted(global_config.servers.items())
        )
    project_config = await load_mcp_config(project_path)
    effective = {
        server_id: ConfiguredMcpServer(server_id, "global", config)
        for server_id, config in global_config.servers.items()
    }
    effective.update(
        {
            server_id: ConfiguredMcpServer(server_id, "project", config)
            for server_id, config in project_config.servers.items()
        }
    )
    return tuple(effective[server_id] for server_id in sorted(effective))


def _save_mcp_config(path: Path, config: McpConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            config.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _safe_validation_reason(error: ValidationError) -> str:
    details = error.errors(include_url=False, include_input=False)
    if any(detail["type"] == "json_invalid" for detail in details):
        return "invalid JSON"
    locations = sorted(
        ".".join(str(component) for component in detail["loc"]) or "root" for detail in details
    )
    location_summary = ", ".join(locations)
    return f"invalid MCP configuration at {location_summary}"


def _validate_cli_server_id(server_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", server_id):
        raise McpConfigMutationError(
            "MCP server IDs must contain only letters, digits, underscores, or hyphens"
        )
    if len(server_id) > 64:
        raise McpConfigMutationError("MCP server IDs must not exceed 64 characters")
