"""定义 MCP 启动 Event、诊断以及规范化的 Resource 和 Prompt 值。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from phi.harness import Event


@dataclass(frozen=True)
class McpServerConnected(Event):
    """一个 MCP server 已初始化，并完成全部 Tool 注册。"""

    server_id: str
    tool_count: int


@dataclass(frozen=True)
class McpServerConnectFailed(Event):
    """一个 MCP server 启动失败，但未阻塞其余运行时。"""

    server_id: str
    error: str


type McpEvent = McpServerConnected | McpServerConnectFailed


@dataclass(frozen=True)
class McpDiagnostic:
    """一个已配置 MCP server 的安全启动诊断。"""

    server_id: str
    reason: str

    def __str__(self) -> str:
        """渲染不含配置 secret 的用户可读诊断。"""

        return f"MCP server {self.server_id!r}: {self.reason}"


@dataclass(frozen=True)
class McpResource:
    """从已连接 MCP server 缓存的一条具体 Resource 元数据。"""

    server_id: str
    uri: str
    name: str
    description: str | None
    mime_type: str | None


@dataclass(frozen=True)
class McpPromptArgument:
    """用户选择 MCP Prompt 时可提供的一个参数。"""

    name: str
    description: str | None
    required: bool


@dataclass(frozen=True)
class McpPrompt:
    """带稳定用户命令标识的缓存 Prompt 元数据。"""

    server_id: str
    name: str
    description: str | None
    command: str
    arguments: tuple[McpPromptArgument, ...]


@dataclass(frozen=True)
class McpPromptMessage:
    """一次可信 Prompt 选择返回的规范化消息。"""

    role: str
    content: Mapping[str, Any]


@dataclass(frozen=True)
class McpPromptResult:
    """提供给可信运行时调用方的规范化 MCP Prompt 输出。"""

    description: str | None
    messages: tuple[McpPromptMessage, ...]


class McpPromptError(LookupError):
    """可信调用方请求的 Prompt 操作无法完成。"""
