from __future__ import annotations

import json
from collections.abc import Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Static

from phi.harness import ContextInspection
from phi.mcp import McpPrompt
from phi.model import ToolCall
from phi.sessions import SessionHandle
from phi.tools import AskResolution, Tool


class SelectionScreen(ModalScreen[str | None]):
    """Deterministic keyboard-accessible selector for Host operations."""

    CSS = """
    SelectionScreen { align: center middle; }
    #selection-dialog {
        width: 84%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #selection-options { height: auto; max-height: 30; }
    #selection-options Button { width: 100%; margin-bottom: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, options: tuple[tuple[str, str], ...]) -> None:
        super().__init__()
        self.selection_title = title
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="selection-dialog"):
            yield Static(self.selection_title, classes="screen-title", markup=False)
            with VerticalScroll(id="selection-options"):
                for index, (label, _value) in enumerate(self.options):
                    yield Button(Text(label), id=f"selection-{index}")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id is None or not button_id.startswith("selection-"):
            return
        index = int(button_id.removeprefix("selection-"))
        self.dismiss(self.options[index][1])


class PromptArgumentsScreen(ModalScreen[dict[str, str] | None]):
    """Collect advertised string arguments before retrieving one MCP Prompt."""

    CSS = """
    PromptArgumentsScreen { align: center middle; }
    #prompt-arguments-dialog {
        width: 78%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #prompt-arguments-dialog Input { margin-bottom: 1; }
    #prompt-argument-error { color: $error; height: auto; }
    #prompt-argument-actions { height: auto; }
    #prompt-argument-actions Button { width: 1fr; margin: 0 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, prompt: McpPrompt, initial: dict[str, str]) -> None:
        super().__init__()
        self.prompt = prompt
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-arguments-dialog"):
            yield Static(self.prompt.command, classes="screen-title", markup=False)
            for index, argument in enumerate(self.prompt.arguments):
                label = f"{argument.name}{' (required)' if argument.required else ' (optional)'}"
                yield Static(label, markup=False)
                yield Input(
                    value=self.initial.get(argument.name, ""),
                    placeholder=argument.description or argument.name,
                    id=f"prompt-argument-{index}",
                )
            yield Static("", id="prompt-argument-error")
            with Horizontal(id="prompt-argument-actions"):
                yield Button("Retrieve", id="prompt-arguments-submit", variant="primary")
                yield Button("Cancel", id="prompt-arguments-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-arguments-cancel":
            self.dismiss(None)
            return
        if event.button.id != "prompt-arguments-submit":
            return
        values: dict[str, str] = {}
        missing: list[str] = []
        for index, argument in enumerate(self.prompt.arguments):
            value = self.query_one(f"#prompt-argument-{index}", Input).value
            if argument.required and not value.strip():
                missing.append(argument.name)
            elif value:
                values[argument.name] = value
        if missing:
            self.query_one("#prompt-argument-error", Static).update(
                f"Required: {', '.join(missing)}"
            )
            return
        self.dismiss(values)


class InfoScreen(ModalScreen[None]):
    """Keyboard-dismissable detail presentation for read-only Host commands."""

    CSS = """
    InfoScreen { align: center middle; }
    #info-dialog {
        width: 82%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #info-content { height: auto; }
    #info-close { width: 100%; margin-top: 1; }
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.info_title = title
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="info-dialog"):
            yield Static(self.info_title, classes="screen-title", markup=False)
            yield Static(self.content, id="info-content", markup=False)
            yield Button("Close", id="info-close", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "info-close":
            self.dismiss(None)


class ApprovalPromptScreen(ModalScreen[AskResolution]):
    """Fail-closed human decision boundary for one proposed Tool Call."""

    CSS = """
    ApprovalPromptScreen { align: center middle; }
    #approval-dialog {
        width: 80%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #approval-dialog Static { height: auto; margin-bottom: 1; }
    #approval-actions { height: auto; }
    #approval-actions Button { width: 1fr; margin: 0 1; }
    """

    BINDINGS = [("escape", "deny", "Deny")]

    def __init__(self, call: ToolCall, tool: Tool) -> None:
        super().__init__()
        self.call = call
        self.tool = tool

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Static(f"Tool: {self.tool.name}", id="approval-tool", markup=False)
            yield Static(
                f"Approval class: {self.tool.approval_class.value}",
                id="approval-class",
                markup=False,
            )
            yield Static(
                json.dumps(self.call.arguments, ensure_ascii=False, indent=2, sort_keys=True),
                id="approval-arguments",
                markup=False,
            )
            with Horizontal(id="approval-actions"):
                yield Button("Allow once", id="approval-once", variant="success")
                yield Button("Allow for session", id="approval-session", variant="primary")
                yield Button("Deny", id="approval-deny", variant="error")

    def action_deny(self) -> None:
        self.dismiss(AskResolution.DENY)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        decisions = {
            "approval-once": AskResolution.ALLOW_ONCE,
            "approval-session": AskResolution.ALLOW_FOR_SESSION,
            "approval-deny": AskResolution.DENY,
        }
        self.dismiss(decisions.get(event.button.id, AskResolution.DENY))


class ContextInspectorScreen(ModalScreen[None]):
    """Complete, collapsible presentation of the exact next Context."""

    CSS = """
    ContextInspectorScreen { align: center middle; }
    #context-dialog {
        width: 92%;
        height: 92%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #context-dialog Collapsible { margin: 1 0; }
    #context-dialog Static { height: auto; }
    #context-close { width: 100%; margin-top: 1; }
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(
        self,
        inspection: ContextInspection,
        handle: SessionHandle,
        *,
        provider_usage: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__()
        self.inspection = inspection
        self.handle = handle
        self.provider_usage = provider_usage

    def compose(self) -> ComposeResult:
        context = self.inspection.context
        anchor = self.handle.prompt_budget_anchor
        usage = "Aggregate provider Usage: unavailable"
        if self.provider_usage is not None:
            usage = f"Aggregate provider Usage: {dict(self.provider_usage)}"
        if anchor is not None:
            usage = f"{usage}\nLatest prompt Usage anchor: {anchor.prompt_tokens} tokens"
        budget = (
            f"Character counts: {dict(self.inspection.character_counts)}\n"
            f"Token Estimate: {self.inspection.estimate.tokens}\n"
            f"Local Token Estimate: {self.inspection.estimate.local_tokens}\n"
            f"Provider anchor contributed: {self.inspection.estimate.used_provider_anchor}\n"
            f"{usage}\n"
            f"Effective input limit: {self.inspection.effective_input_limit or 'unknown'}\n"
            f"Safe input limit: {self.inspection.safe_prompt_limit or 'unknown'}"
        )
        with VerticalScroll(id="context-dialog"):
            yield Static("Context Inspector", classes="screen-title")
            yield Collapsible(
                Static(context.system_prompt, id="context-system", markup=False),
                title="System prompt",
                collapsed=False,
            )
            yield Collapsible(
                Static(
                    json.dumps(list(context.tools), ensure_ascii=False, indent=2, sort_keys=True),
                    id="context-tools",
                    markup=False,
                ),
                title="Tools",
            )
            yield Collapsible(
                Static(
                    json.dumps(
                        list(context.messages),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    id="context-messages",
                    markup=False,
                ),
                title="Messages",
                collapsed=False,
            )
            yield Collapsible(
                Static(context.dropped_summary or "(none)", id="context-summary", markup=False),
                title="Dropped history summary",
            )
            yield Collapsible(
                Static(budget, id="context-budget", markup=False),
                title="Counts and budget",
                collapsed=False,
            )
            yield Button("Close", id="context-close", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "context-close":
            self.dismiss(None)
