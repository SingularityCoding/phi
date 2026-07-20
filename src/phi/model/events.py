"""定义 Model 流在协议归一化后产生的增量事件。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from phi.model.types import Usage


@dataclass(frozen=True)
class ContentDelta:
    """表示一段用户可见的 Model 内容。"""

    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    """表示一段提供方报告的 Model 推理内容。"""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """表示可能被拆分传输的 Tool Call 的一个分片。"""

    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""


@dataclass(frozen=True)
class FinishEvent:
    """表示提供方报告的 Model 响应结束原因。"""

    finish_reason: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class UsageEvent:
    """表示提供方报告的 Usage；它可能晚于结束分片到达。"""

    usage: Usage
    raw: dict[str, Any]


# 联合类型明确限定 Model 流可跨越协议边界的事件集合。
type ModelEvent = ContentDelta | ReasoningDelta | ToolCallDelta | FinishEvent | UsageEvent
