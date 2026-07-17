from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr


@dataclass(frozen=True)
class ModelConfig:
    """Trusted configuration required by an OpenAI-compatible Model."""

    base_url: str
    api_key: SecretStr
    default_model: str
    request_timeout_seconds: float


@dataclass(frozen=True)
class ModelRequest:
    """One stateless request sent to a Model."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class ToolCall:
    """A Tool Call proposed by a Model with normalized arguments."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The Harness-produced outcome of processing a Tool Call."""

    call_id: str
    output: str
    error: str | None = None


@dataclass(frozen=True)
class Usage:
    """Provider-reported token counts for one completed Model request."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    """The transport-independent result of one Model request."""

    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    """Trustworthy metadata for a Model available through the Proxy."""

    id: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
