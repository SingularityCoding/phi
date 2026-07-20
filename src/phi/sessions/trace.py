"""把 Run Events 脱敏后写入独立 Trace；Trace 永不用于恢复 Session。"""

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
    """一个 Session 的尽力而为、已脱敏 JSONL Event 消费器。"""

    def __init__(self, path: Path) -> None:
        """绑定 Trace 路径并初始化并发安全的写缓冲。"""

        self.path = path
        self._lock = asyncio.Lock()
        self._pending: list[str] = []

    async def __call__(self, event: RunEvent) -> None:
        """序列化 Event，并按流式增量批量或立即落盘。"""

        record = serialize_run_event(event)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        async with self._lock:
            self._pending.append(line)
            # 高频 Model delta 先成批写入；其他边界 Event 会顺带刷出之前的增量。
            if isinstance(event, ModelCallDelta) and len(self._pending) < _DELTA_BATCH_SIZE:
                return
            await self._flush_locked()

    async def flush(self) -> None:
        """显式刷出仍在缓冲区中的 Trace 记录。"""

        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """在调用方持锁时转移缓冲，并在线程中执行阻塞磁盘写入。"""

        if not self._pending:
            return
        lines = tuple(self._pending)
        self._pending.clear()
        await asyncio.to_thread(self._append_sync, lines)

    def _append_sync(self, lines: tuple[str, ...]) -> None:
        """追加一批完整 JSONL 记录并 fsync。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            for line in lines:
                file.write(line)
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())


def serialize_run_event(event: RunEvent) -> dict[str, Any]:
    """把一个 Run Event 转成 Trace/Host 共用的脱敏记录 schema。"""

    record: dict[str, Any] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "event_type": _event_type(event),
        "run_id": event.run_id,
        "event_index": event.event_index,
    }
    step_index = getattr(event, "step_index", None)
    if isinstance(step_index, int):
        record["step_index"] = step_index
    # 先构造语义 payload，再递归脱敏，避免某个 Event 分支遗漏安全边界。
    record["payload"] = _redact(_event_payload(event))
    return record


def _event_type(event: RunEvent) -> str:
    """将类型化 Event 映射成稳定的持久化名称。"""

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
    """提取各 Event 的可序列化观测数据，不包含公共信封字段。"""

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
    """把流式 Model delta 归一化为带 discriminator 的字典。"""

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
    """将 Tool Call 投影为 Trace 可序列化字段。"""

    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def _tool_result(result: ToolResult) -> dict[str, Any]:
    """将 Tool Result 投影为 Trace 可序列化字段。"""

    return {"call_id": result.call_id, "output": result.output, "error": result.error}


def _usage(usage: Usage | None) -> dict[str, int | None] | None:
    """保留 provider 报告的各类 Usage 计数。"""

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
    """只记录异常类型与脱敏后的消息，不序列化异常对象。"""

    if error is None:
        return None
    return {"type": type(error).__name__, "message": redact_text(str(error))}


def _redact(value: Any, *, key: str | None = None) -> Any:
    """递归遍历 Event payload，并对键名和字符串内容双重脱敏。"""

    # 命中凭据语义的字段整值替换，避免嵌套结构或非字符串值泄漏。
    if key is not None and _is_credential_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def _is_credential_key(key: str) -> bool:
    """在统一键名格式后识别常见凭据字段。"""

    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in _CREDENTIAL_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def redact_text(value: str, *, max_length: int | None = _MAX_TEXT_LENGTH) -> str:
    """脱敏形似凭据的文本，并可限制安全可见输出的长度。"""

    # 规则依次覆盖 Bearer、带引号键值、裸键值和常见 sk- 前缀密钥。
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _QUOTED_CREDENTIAL_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}",
        redacted,
    )
    redacted = _UNQUOTED_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _KEY_PATTERN.sub("[REDACTED]", redacted)
    if max_length is not None and len(redacted) > max_length:
        return redacted[:max_length] + "…"
    return redacted
