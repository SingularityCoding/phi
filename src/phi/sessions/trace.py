from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from phi.harness import (
    ApprovalDecided,
    ModelCallCompleted,
    ModelCallDelta,
    ModelCallStarted,
    RunEvent,
    RunFinished,
    RunStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from phi.model import (
    ContentDelta,
    FinishEvent,
    ReasoningDelta,
    ToolCall,
    ToolCallDelta,
    ToolResult,
    Usage,
    UsageEvent,
)

TRACE_SCHEMA_VERSION = 1
_MAX_TEXT_LENGTH = 16_384
_DELTA_BATCH_SIZE = 64
_CREDENTIAL_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_QUOTED_CREDENTIAL_PATTERN = re.compile(
    r"""(?ix)
    (['"]?(?:api[-_]?key|authorization|cookie|password|secret|[a-z0-9_-]*token)['"]?
    \s*[:=]\s*)(['"])(.*?)(\2)
    """
)
_UNQUOTED_CREDENTIAL_PATTERN = re.compile(
    r"""(?ix)
    (['"]?(?:api[-_]?key|authorization|cookie|password|secret|[a-z0-9_-]*token)['"]?
    \s*[:=]\s*)(?!['"])([^\s,;}]+)
    """
)


class TraceWriter:
    """Best-effort redacted JSONL Event consumer for one Session."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._pending: list[str] = []

    async def __call__(self, event: RunEvent) -> None:
        record = _event_record(event)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        async with self._lock:
            self._pending.append(line)
            if isinstance(event, ModelCallDelta) and len(self._pending) < _DELTA_BATCH_SIZE:
                return
            await self._flush_locked()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._pending:
            return
        lines = tuple(self._pending)
        self._pending.clear()
        await asyncio.to_thread(self._append_sync, lines)

    def _append_sync(self, lines: tuple[str, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            for line in lines:
                file.write(line)
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())


def _event_record(event: RunEvent) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "event_type": _event_type(event),
        "run_id": event.run_id,
        "event_index": event.event_index,
    }
    step_index = getattr(event, "step_index", None)
    if isinstance(step_index, int):
        record["step_index"] = step_index
    record["payload"] = _redact(_event_payload(event))
    return record


def _event_type(event: RunEvent) -> str:
    if isinstance(event, RunStarted):
        return "run_started"
    if isinstance(event, ModelCallStarted):
        return "model_call_started"
    if isinstance(event, ModelCallDelta):
        return "model_call_delta"
    if isinstance(event, ModelCallCompleted):
        return "model_call_completed"
    if isinstance(event, ToolCallStarted):
        return "tool_call_started"
    if isinstance(event, ToolCallCompleted):
        return "tool_call_completed"
    if isinstance(event, ApprovalDecided):
        return "approval_decided"
    if isinstance(event, RunFinished):
        return "run_finished"
    raise TypeError(f"unsupported Run Event {type(event).__name__}")


def _event_payload(event: RunEvent) -> dict[str, Any]:
    if isinstance(event, RunStarted):
        return {}
    if isinstance(event, ModelCallStarted):
        return {
            "request": {
                "messages": event.request.messages,
                "tools": event.request.tools,
                "model": event.request.model,
                "temperature": event.request.temperature,
                "max_tokens": event.request.max_tokens,
            }
        }
    if isinstance(event, ModelCallDelta):
        return {"delta": _model_delta(event.delta)}
    if isinstance(event, ModelCallCompleted):
        response = event.response
        return {
            "response": {
                "content": response.content,
                "reasoning": response.reasoning,
                "tool_calls": [_tool_call(call) for call in response.tool_calls],
                "usage": _usage(response.usage),
                "finish_reason": response.finish_reason,
            },
            "latency_seconds": event.latency_seconds,
        }
    if isinstance(event, ToolCallStarted):
        return {"call": _tool_call(event.call)}
    if isinstance(event, ToolCallCompleted):
        return {
            "call": _tool_call(event.call),
            "result": _tool_result(event.result),
            "latency_seconds": event.latency_seconds,
        }
    if isinstance(event, ApprovalDecided):
        return {
            "call": _tool_call(event.call),
            "decision": event.decision.value,
            "mode": event.mode,
        }
    if isinstance(event, RunFinished):
        return {
            "status": event.result.status.value,
            "output": event.result.output,
            "error": _error(event.result.error),
            "step_count": len(event.result.steps),
        }
    raise TypeError(f"unsupported Run Event {type(event).__name__}")


def _model_delta(delta: object) -> dict[str, Any]:
    if isinstance(delta, ContentDelta):
        return {"delta_type": "content", "content": delta.text}
    if isinstance(delta, ReasoningDelta):
        return {"delta_type": "reasoning", "reasoning": delta.text}
    if isinstance(delta, ToolCallDelta):
        return {
            "delta_type": "tool_call",
            "index": delta.index,
            "id": delta.id,
            "name": delta.name,
            "arguments_fragment": delta.arguments_fragment,
        }
    if isinstance(delta, UsageEvent):
        return {"delta_type": "usage", "usage": _usage(delta.usage)}
    if isinstance(delta, FinishEvent):
        return {"delta_type": "finish", "finish_reason": delta.finish_reason}
    return {"delta_type": type(delta).__name__}


def _tool_call(call: ToolCall) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def _tool_result(result: ToolResult) -> dict[str, Any]:
    return {"call_id": result.call_id, "output": result.output, "error": result.error}


def _usage(usage: Usage | None) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": usage.cached_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


def _error(error: Exception | None) -> dict[str, str] | None:
    if error is None:
        return None
    return {"type": type(error).__name__, "message": _safe_text(str(error))}


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_credential_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(str(value))


def _is_credential_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in _CREDENTIAL_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def _safe_text(value: str) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _QUOTED_CREDENTIAL_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}",
        redacted,
    )
    redacted = _UNQUOTED_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _KEY_PATTERN.sub("[REDACTED]", redacted)
    if len(redacted) > _MAX_TEXT_LENGTH:
        return redacted[:_MAX_TEXT_LENGTH] + "…"
    return redacted
