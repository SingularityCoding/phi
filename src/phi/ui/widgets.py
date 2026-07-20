from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from rich.rule import Rule
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import (
    Button,
    Collapsible,
    LoadingIndicator,
    Markdown,
    Static,
    TextArea,
)

from phi.sessions import redact_text


class QueueDisposition(StrEnum):
    QUEUE = "queue"
    STEER = "steer"


@dataclass
class PendingMessage:
    id: str
    text: str
    disposition: QueueDisposition = QueueDisposition.QUEUE
    target_run_generation: int | None = None


class PromptInput(TextArea):
    """Multiline request editor whose plain Enter submits."""

    MIN_HEIGHT = 3
    MAX_HEIGHT = 8

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    BINDINGS = [
        Binding("shift+enter", "newline", priority=True),
        Binding("enter", "submit", priority=True),
    ]

    def action_newline(self) -> None:
        self.insert("\n", maintain_selection_offset=False)

    def action_submit(self) -> None:
        text = self.text
        self.load_text("")
        self.post_message(self.Submitted(text))

    def resize_for_content(self) -> None:
        """Grow with wrapped content while preserving transcript space."""
        content_height = max(1, self.virtual_size.height)
        self.styles.height = min(
            max(content_height + 2, self.MIN_HEIGHT),
            self.MAX_HEIGHT,
        )


class TranscriptView(VerticalScroll):
    """Scrollable presentation of durable Entries and live Run Events."""


class UserMessageView(Static):
    def __init__(self, content: str) -> None:
        super().__init__(f"You\n{content}", classes="user-message", markup=False)


class AssistantMessageView(Markdown):
    def __init__(self, content: str = "") -> None:
        self._content = content
        super().__init__(self._source(), classes="assistant-message")

    def _source(self) -> str:
        return f"### Phi\n\n{self._content}"

    def set_content(self, content: str) -> None:
        self._content = content
        self.update(self._source())

    def append_content(self, content: str) -> None:
        self.set_content(f"{self._content}{content}")

    @property
    def is_empty(self) -> bool:
        return not self._content


class ReasoningView(Collapsible):
    DEFAULT_CSS = """
    ReasoningView {
        width: 1fr;
        height: auto;
        margin: 1 2 0 2;
        padding: 0;
        border: none;
        background: transparent;
        color: $text-muted;
    }
    ReasoningView:focus-within { background-tint: transparent; }
    ReasoningView CollapsibleTitle {
        padding: 0 1;
        color: $text-muted;
        text-style: none;
        background: transparent;
    }
    ReasoningView CollapsibleTitle:hover {
        color: $text;
        background: $panel;
    }
    ReasoningView CollapsibleTitle:focus {
        color: $accent;
        text-style: bold;
        background: $panel;
    }
    ReasoningView Contents {
        width: 100%;
        height: auto;
        margin: 1 0 0 1;
        padding: 0 0 0 1;
        border-left: solid $secondary;
    }
    ReasoningView .reasoning-content {
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(self, content: str = "") -> None:
        self._content = content
        self._body = Static(content, classes="reasoning-content", markup=False)
        super().__init__(
            self._body,
            title="Reasoning",
            collapsed=True,
            collapsed_symbol="▸",
            expanded_symbol="▾",
            classes="reasoning-message",
        )
        self.display = bool(content)

    def set_content(self, content: str) -> None:
        self._content = content
        self._body.update(content)
        self.display = bool(content)

    def append_content(self, content: str) -> None:
        self.set_content(f"{self._content}{content}")

    @property
    def is_empty(self) -> bool:
        return not self._content


class ToolCallView(Vertical):
    def __init__(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        self.call_id = call_id
        self.tool_name = name
        self.arguments = arguments
        self._content = Static(
            self._render_running(),
            classes="tool-call-content",
            markup=False,
        )
        self._progress = LoadingIndicator(classes="tool-call-progress")
        super().__init__(
            self._content,
            self._progress,
            classes="tool-call running",
        )

    def _render_running(self) -> str:
        return f"Tool · {self.tool_name} · running\nArguments: {self.arguments}"

    def complete(self, output: str, error: str | None) -> None:
        self.remove_class("running")
        self.add_class("failed" if error is not None else "completed")
        self._progress.display = False
        outcome = f"Error: {redact_text(error)}" if error is not None else f"Result: {output}"
        self._content.update(
            f"Tool · {self.tool_name} · complete\nArguments: {self.arguments}\n{outcome}"
        )


class CompactionEntryView(Static):
    def __init__(self, summary: str) -> None:
        super().__init__(
            f"Compaction\n{summary}",
            classes="compaction-entry",
            markup=False,
        )


class RunStatusView(Static):
    def __init__(self, text: str, *, failed: bool = False) -> None:
        super().__init__(
            text,
            classes=f"run-status{' failed' if failed else ''}",
            markup=False,
        )


class RunBoundaryView(Static):
    """Low-emphasis visual boundary after one terminal Run outcome."""

    DEFAULT_CSS = """
    RunBoundaryView {
        width: 1fr;
        height: 1;
        margin: 1 2;
        color: $text-muted;
        text-style: dim;
    }
    RunBoundaryView.warning {
        color: $warning;
        text-style: none;
    }
    """

    def __init__(self, finished_at: datetime, *, status_label: str | None = None) -> None:
        local_finished_at = finished_at.astimezone()
        self.time_label = local_finished_at.strftime("%H:%M")
        self.status_label = status_label
        self.boundary_title = (
            self.time_label if status_label is None else f"{status_label} · {self.time_label}"
        )
        classes = "run-boundary completed" if status_label is None else "run-boundary warning"
        super().__init__("", classes=classes, markup=False)

    def render(self) -> Rule:
        style = self.rich_style
        return Rule(
            Text(self.boundary_title, style=style),
            characters="─",
            style=style,
        )


class QueueMessageRow(Horizontal):
    def __init__(self, pending: PendingMessage) -> None:
        self.pending_id = pending.id
        super().__init__(
            Static(
                self._label(pending),
                id=f"queued-text-{pending.id}",
                classes="queued-message",
                markup=False,
            ),
            Button("Edit", id=f"edit-{pending.id}", compact=True),
            Button("Steer", id=f"toggle-{pending.id}", compact=True),
            Button("Remove", id=f"remove-{pending.id}", compact=True),
            id=f"pending-{pending.id}",
            classes="queue-row",
        )

    @staticmethod
    def _label(pending: PendingMessage) -> str:
        disposition = "Queue" if pending.disposition is QueueDisposition.QUEUE else "Steer"
        preview = " ".join(pending.text.split())
        return f"{disposition} · {preview}"

    def refresh_pending(self, pending: PendingMessage) -> None:
        self.query_one(f"#queued-text-{pending.id}", Static).update(self._label(pending))
        toggle = self.query_one(f"#toggle-{pending.id}", Button)
        toggle.label = "Queue" if pending.disposition is QueueDisposition.STEER else "Steer"
