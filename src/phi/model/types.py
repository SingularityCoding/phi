"""定义 Model 边界内部使用的可信、传输无关值对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr


@dataclass(frozen=True)
class ModelConfig:
    """保存 OpenAI 兼容 Model 所需的可信配置。"""

    base_url: str
    api_key: SecretStr
    default_model: str
    request_timeout_seconds: float


@dataclass(frozen=True)
class ModelRequest:
    """表示发送给 Model 的一次无状态请求。"""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class ToolCall:
    """表示 Model 提议且参数已归一化的 Tool Call。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """表示 Harness 处理 Tool Call 后产生的 Tool Result。"""

    call_id: str
    output: str
    error: str | None = None


@dataclass(frozen=True)
class Usage:
    """保存提供方为一次已完成 Model 请求报告的 token 计数。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    """表示一次 Model 请求的传输无关结果。"""

    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    """保存 Proxy 所暴露 Model 的可信元数据。"""

    id: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
