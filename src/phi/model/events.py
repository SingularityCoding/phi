from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from phi.model.types import Usage


@dataclass(frozen=True)
class ContentDelta:
    """A fragment of visible Model content."""

    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    """A fragment of provider-reported Model reasoning."""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """One fragment of a possibly chunked Tool Call."""

    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""


@dataclass(frozen=True)
class FinishEvent:
    """The finish reason reported for a Model response."""

    finish_reason: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class UsageEvent:
    """Provider-reported Usage, which may arrive after the finish chunk."""

    usage: Usage
    raw: dict[str, Any]


type ModelEvent = ContentDelta | ReasoningDelta | ToolCallDelta | FinishEvent | UsageEvent
