"""Immutable presentation model for one read-only Context inspection."""

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
    """Meaningful counts across the Session-to-request projection."""

    session_path_entries: int
    conversation_view_entries: int
    context_messages: int
    request_messages: int


@dataclass(frozen=True)
class InspectedTool:
    """One registered Tool represented in an immutable Context snapshot."""

    name: str
    description: str
    schema: Mapping[str, Any]
    characters: int
    provenance: str = "Tool Registry"
    inclusion: str = "Registered · included"


@dataclass(frozen=True)
class InspectedMessage:
    """One selected Model-visible message with semantic presentation metadata."""

    index: int
    label: str
    readable_content: str
    message: Mapping[str, Any]
    characters: int
    provenance: str = "Conversation View"
    inclusion: str = "Selected · included"


@dataclass(frozen=True)
class InspectedSummary:
    """Generated representation of Entries omitted from the finite Context."""

    content: str
    characters: int
    provenance: str = "Compaction"
    inclusion: str = "Generated · included"


@dataclass(frozen=True)
class ContextInspection:
    """Complete inspectable projection and budget diagnostics for one Context."""

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
        object.__setattr__(self, "request", freeze_request(self.request))

    @property
    def character_counts(self) -> Mapping[str, int]:
        return MappingProxyType(self.context.character_counts)

    @property
    def utilization_percent(self) -> float | None:
        if self.effective_input_limit is None:
            return None
        return self.estimate.tokens / self.effective_input_limit * 100
