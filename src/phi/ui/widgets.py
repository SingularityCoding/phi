from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

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


class QueueDisposition(StrEnum):
    QUEUE = "queue"
    STEER = "steer"


@dataclass
class PendingMessage:
    id: str
    text: str
    disposition: QueueDisposition = QueueDisposition.QUEUE


class PromptInput(TextArea):
    """Multiline request editor whose plain Enter submits."""

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
    def __init__(self, content: str = "") -> None:
        self._content = content
        self._body = Static(content, classes="reasoning-content", markup=False)
        super().__init__(
            self._body,
            title="Reasoning",
            collapsed=True,
            classes="reasoning-message",
        )

    def set_content(self, content: str) -> None:
        self._content = content
        self._body.update(content)

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
        outcome = f"Error: {error}" if error is not None else f"Result: {output}"
        self._content.update(f"Tool · {self.tool_name} · complete\n{outcome}")


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
        return f"Pending · {pending.disposition.value}\n{pending.text}"

    def refresh_pending(self, pending: PendingMessage) -> None:
        self.query_one(f"#queued-text-{pending.id}", Static).update(self._label(pending))
        toggle = self.query_one(f"#toggle-{pending.id}", Button)
        toggle.label = "Queue" if pending.disposition is QueueDisposition.STEER else "Steer"
