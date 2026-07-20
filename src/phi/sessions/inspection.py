"""一次只读 Context 检查所使用的不可变展示模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from phi.harness.compaction import PromptEstimate
from phi.harness.context import Context
from phi.harness.snapshots import freeze_request
from phi.instructions import InstructionSection
from phi.model import ModelRequest


@dataclass(frozen=True)
class ProjectionCounts:
    """从 Session 到 Model 请求的各层投影数量。"""

    session_path_entries: int
    conversation_view_entries: int
    context_messages: int
    request_messages: int


@dataclass(frozen=True)
class InspectedTool:
    """不可变 Context 快照中的一个已注册 Tool。"""

    name: str
    description: str
    schema: Mapping[str, Any]
    characters: int
    provenance: str = "Tool Registry"
    inclusion: str = "Registered · included"


@dataclass(frozen=True)
class InspectedMessage:
    """一条选入 Context 的 Model 可见消息及其语义展示信息。"""

    index: int
    label: str
    readable_content: str
    message: Mapping[str, Any]
    characters: int
    provenance: str = "Conversation View"
    inclusion: str = "Selected · included"


@dataclass(frozen=True)
class InspectedSummary:
    """有限 Context 中被省略 Entries 的生成式表示。"""

    content: str
    characters: int
    provenance: str = "Compaction"
    inclusion: str = "Generated · included"


@dataclass(frozen=True)
class ContextInspection:
    """一个 Context 的完整可检查投影与预算诊断。"""

    context: Context
    request: ModelRequest
    model_id: str | None
    projection: ProjectionCounts
    instructions: tuple[InstructionSection, ...]
    tools: tuple[InspectedTool, ...]
    messages: tuple[InspectedMessage, ...]
    dropped_summary: InspectedSummary | None
    estimate: PromptEstimate
    provider_anchor_prompt_tokens: int | None
    effective_input_limit: int | None
    safe_prompt_limit: int | None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """冻结请求中的嵌套容器，防止检查结果随后被调用方改写。"""

        object.__setattr__(self, "request", freeze_request(self.request))

    @property
    def character_counts(self) -> Mapping[str, int]:
        """返回按 Context 组成部分统计的只读字符数。"""

        return MappingProxyType(self.context.character_counts)

    @property
    def utilization_percent(self) -> float | None:
        """计算估算 prompt 占有效输入上限的百分比。"""

        if self.effective_input_limit is None:
            return None
        return self.estimate.tokens / self.effective_input_limit * 100
