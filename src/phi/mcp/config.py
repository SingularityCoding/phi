"""加载、合并并通过原子文件替换修改项目级与全局 MCP JSON 配置。"""

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
    """针对一个 MCP 配置来源的安全、可行动诊断。"""

    source_path: Path
    reason: str

    def __str__(self) -> str:
        """以来源路径加原因的形式渲染诊断。"""

        return f"{self.source_path}: {self.reason}"


class McpConfigError(ValueError):
    """一个存在的 MCP 配置来源无法被安全读写。"""

    def __init__(self, diagnostic: McpConfigDiagnostic) -> None:
        """保留结构化诊断，同时提供标准异常消息。"""

        self.diagnostic = diagnostic
        super().__init__(str(diagnostic))


class McpConfigMutationError(ValueError):
    """针对单一配置来源的修改请求无效。"""


class McpServerConfig(BaseModel):
    """从 JSON 边界解析的一条不可信 stdio MCP server 定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def _command_must_not_be_blank(cls, value: str) -> str:
        """拒绝只含空白的可执行命令。"""

        if not value.strip():
            raise ValueError("command must not be blank")
        return value


class McpConfig(BaseModel):
    """一个经过严格验证的 MCP 配置来源。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")


@dataclass(frozen=True)
class ConfiguredMcpServer:
    """带最终来源信息的有效 MCP server 定义。"""

    server_id: str
    source: Literal["project", "global"]
    config: McpServerConfig


async def load_mcp_config(path: Path) -> McpConfig:
    """加载一个 MCP 配置来源；文件不存在等价于空配置。"""

    if not await asyncio.to_thread(path.exists):
        return McpConfig()
    try:
        # 配置文件 I/O 放在线程中，避免阻塞 Host 的事件循环。
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
    """合并全局与项目配置；同名 server 由项目定义完整替换。"""

    global_config = await load_mcp_config(global_path)
    project_config = await load_mcp_config(project_path)
    return McpConfig(mcpServers={**global_config.servers, **project_config.servers})


async def save_mcp_config(path: Path, config: McpConfig) -> None:
    """以确定性 JSON 形式原子保存一个选定配置来源。"""

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
    """验证并通过原子文件替换向选定来源添加 stdio server 定义。"""

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
    """只从明确选定的来源中删除 server 定义，并原子替换配置文件。"""

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
    """返回仅全局或项目覆盖全局后的有效定义，并标注来源。"""

    global_config = await load_mcp_config(global_path)
    if global_only:
        return tuple(
            ConfiguredMcpServer(server_id, "global", config)
            for server_id, config in sorted(global_config.servers.items())
        )
    project_config = await load_mcp_config(project_path)
    # 先建立全局视图，再由 update 替换同名项，从而保留最终来源信息。
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
    """同步写临时文件、fsync 后替换目标，避免留下半份 JSON。"""

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
        # os.replace 在同一文件系统内提供目标路径级的原子替换。
        os.replace(temporary_path, path)
    except BaseException:
        # 同步写入或替换过程中的任何失败都不应遗留临时配置文件。
        temporary_path.unlink(missing_ok=True)
        raise


def _safe_validation_reason(error: ValidationError) -> str:
    """只暴露字段位置，不把可能含 secret 的原始输入写入诊断。"""

    details = error.errors(include_url=False, include_input=False)
    if any(detail["type"] == "json_invalid" for detail in details):
        return "invalid JSON"
    locations = sorted(
        ".".join(str(component) for component in detail["loc"]) or "root" for detail in details
    )
    location_summary = ", ".join(locations)
    return f"invalid MCP configuration at {location_summary}"


def _validate_cli_server_id(server_id: str) -> None:
    """验证 CLI 管理面接受的 server ID 字符集与线长约束。"""

    if not re.fullmatch(r"[A-Za-z0-9_-]+", server_id):
        raise McpConfigMutationError(
            "MCP server IDs must contain only letters, digits, underscores, or hyphens"
        )
    if len(server_id) > 64:
        raise McpConfigMutationError("MCP server IDs must not exceed 64 characters")
