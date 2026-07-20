"""把流式 Model Event 增量组装成统一的 Model Response。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from phi.model.errors import ModelProtocolError
from phi.model.events import (
    ContentDelta,
    FinishEvent,
    ModelEvent,
    ReasoningDelta,
    ToolCallDelta,
    UsageEvent,
)
from phi.model.types import ModelResponse, ToolCall


@dataclass
class _ToolCallBuffer:
    """暂存同一个 Tool Call 跨多个流式分片到达的字段。"""

    id: str | None = None
    name: str | None = None
    arguments: str = ""


class ResponseAssembler:
    """将 Model Event 增量组装成一个归一化响应。"""

    def __init__(self) -> None:
        """初始化各类分片及终止元数据的独立累加器。"""

        # 用“是否观察到字段”区分协议中的 null 与合法的空字符串。
        self._content_parts: list[str] = []
        self._content_observed = False
        self._reasoning_parts: list[str] = []
        self._reasoning_observed = False
        self._tool_calls: dict[int, _ToolCallBuffer] = {}
        self._usage = None
        self._finish_reason: str | None = None
        self._raw: dict[str, Any] = {}

    def absorb(self, event: ModelEvent) -> None:
        """吸收一个 Model Event，并更新对应的累加状态。"""

        # 文本与推理分开累积，避免把仅供观察的推理内容混入最终可见输出。
        if isinstance(event, ContentDelta):
            self._content_observed = True
            self._content_parts.append(event.text)
        elif isinstance(event, ReasoningDelta):
            self._reasoning_observed = True
            self._reasoning_parts.append(event.text)
        elif isinstance(event, ToolCallDelta):
            # index 是分片归属的稳定键；id/name 可能只出现在首个分片。
            buffer = self._tool_calls.setdefault(event.index, _ToolCallBuffer())
            if event.id is not None:
                buffer.id = event.id
            if event.name is not None:
                buffer.name = event.name
            buffer.arguments += event.arguments_fragment
        elif isinstance(event, FinishEvent):
            # finish 与 Usage 可能位于不同 chunk，后到的 raw 代表最终观察到的原始数据。
            self._finish_reason = event.finish_reason
            self._raw = event.raw
        elif isinstance(event, UsageEvent):
            self._usage = event.usage
            self._raw = event.raw

    def build(self) -> ModelResponse:
        """校验所有 Tool Call 缓冲区并构造最终 Model Response。"""

        # 按 index 排序恢复提供方声明的 Tool Call 顺序，再统一解析 JSON 参数。
        tool_calls = [
            self._build_tool_call(index, buffer)
            for index, buffer in sorted(self._tool_calls.items())
        ]
        return ModelResponse(
            content="".join(self._content_parts) if self._content_observed else None,
            reasoning="".join(self._reasoning_parts) if self._reasoning_observed else None,
            tool_calls=tool_calls,
            usage=self._usage,
            finish_reason=self._finish_reason,
            raw=self._raw,
        )

    @staticmethod
    def _build_tool_call(index: int, buffer: _ToolCallBuffer) -> ToolCall:
        """校验一个完整缓冲区并生成结构化 Tool Call。"""

        if buffer.id is None or buffer.name is None:
            raise ModelProtocolError(f"Tool Call at index {index} is missing an id or name")

        # 参数直到流结束才解析；任何半截 JSON 都不能越过 Model 信任边界。
        try:
            arguments: Any = json.loads(
                buffer.arguments,
                parse_constant=_reject_non_json_constant,
            )
        except ValueError as exc:
            raise ModelProtocolError(
                f"Tool Call at index {index} has invalid JSON arguments"
            ) from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError(f"Tool Call at index {index} arguments must be a JSON object")

        return ToolCall(id=buffer.id, name=buffer.name, arguments=arguments)


def _reject_non_json_constant(value: str) -> None:
    """拒绝 Python JSON 解码器默认接受的 NaN 和 Infinity 常量。"""

    raise ValueError(f"{value} is not a valid JSON constant")
