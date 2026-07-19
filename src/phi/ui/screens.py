from __future__ import annotations

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from phi.instructions import InstructionSection
from phi.mcp import McpPrompt
from phi.model import ToolCall
from phi.sessions import (
    ContextInspection,
    InspectedMessage,
    InspectedSummary,
    InspectedTool,
)
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


type ContextContentItem = InstructionSection | InspectedTool | InspectedMessage | InspectedSummary


class ContextContentsTree(Tree[ContextContentItem]):
    """Navigable hierarchy for the Model-input components."""

    def key_1(self) -> None:
        explorer = self._explorer()
        self.call_after_refresh(explorer.action_show_overview)

    def key_2(self) -> None:
        explorer = self._explorer()
        self.call_after_refresh(explorer.action_show_contents)

    def key_3(self) -> None:
        explorer = self._explorer()
        self.call_after_refresh(explorer.action_show_raw)

    def _explorer(self) -> ContextInspectorScreen:
        return self.query_ancestor(ContextInspectorScreen)


class ContextInspectorScreen(Screen[None]):
    """Full-screen read-only explorer for one immutable Model-request snapshot."""

    CSS = """
    ContextInspectorScreen {
        layout: vertical;
        background: $surface;
    }
    #context-explorer-title {
        height: 3;
        padding: 1 2 0 2;
        text-style: bold;
        background: $panel;
    }
    #context-views { height: 1fr; }
    #context-overview-scroll, #context-raw-scroll { padding: 1 2; }
    #context-overview-content, #context-raw-request, #context-content-detail {
        height: auto;
        text-wrap: wrap;
    }
    #context-contents-layout { height: 1fr; layout: horizontal; padding: 1; }
    #context-contents-tree {
        width: 38%;
        min-width: 28;
        border: round $accent;
        padding: 0 1;
    }
    #context-detail-scroll {
        width: 1fr;
        border: round $secondary;
        padding: 1 2;
    }
    ContextInspectorScreen.narrow #context-contents-layout { layout: vertical; }
    ContextInspectorScreen.narrow #context-contents-tree {
        width: 100%;
        min-width: 0;
        height: 45%;
    }
    ContextInspectorScreen.narrow #context-detail-scroll {
        width: 100%;
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("1", "show_overview", "Overview"),
        Binding("2", "show_contents", "Contents"),
        Binding("3", "show_raw", "Raw request"),
        Binding("escape", "close", "Close", priority=True),
    ]

    def __init__(self, inspection: ContextInspection) -> None:
        super().__init__()
        self.inspection = inspection
        self._first_content_node = None

    def compose(self) -> ComposeResult:
        yield Static("Context request explorer", id="context-explorer-title", markup=False)
        with TabbedContent(initial="context-overview", id="context-views"):
            with TabPane("Overview", id="context-overview"):
                with VerticalScroll(id="context-overview-scroll"):
                    yield Static(
                        self._overview_text(),
                        id="context-overview-content",
                        markup=False,
                    )
            with TabPane("Contents", id="context-contents"):
                with Horizontal(id="context-contents-layout"):
                    yield self._contents_tree()
                    with VerticalScroll(id="context-detail-scroll"):
                        yield Static(
                            self._first_content_detail(),
                            id="context-content-detail",
                            markup=False,
                        )
            with TabPane("Raw request", id="context-raw"):
                with VerticalScroll(id="context-raw-scroll"):
                    yield Static(
                        self._raw_request_text(),
                        id="context-raw-request",
                        markup=False,
                    )
        yield Footer()

    def on_mount(self) -> None:
        self.set_class(self.size.width < 80, "narrow")

    def on_resize(self) -> None:
        self.set_class(self.size.width < 80, "narrow")

    def action_show_overview(self) -> None:
        views = self.query_one("#context-views", TabbedContent)
        self.set_focus(None)
        views.active = "context-overview"

    def action_show_contents(self) -> None:
        self.query_one("#context-views", TabbedContent).active = "context-contents"
        tree = self.query_one("#context-contents-tree", Tree)
        tree.focus()
        if self._first_content_node is not None:
            tree.select_node(self._first_content_node)

    def action_show_raw(self) -> None:
        views = self.query_one("#context-views", TabbedContent)
        self.set_focus(None)
        views.active = "context-raw"

    def _contents_tree(self) -> Tree[ContextContentItem]:
        tree = ContextContentsTree("Context contents", id="context-contents-tree")
        tree.show_root = False
        tree.root.expand()
        instructions = tree.root.add("Instructions", expand=True)
        for section in self.inspection.instructions:
            node = instructions.add_leaf(f"{section.origin} · {section.inclusion}", section)
            if self._first_content_node is None:
                self._first_content_node = node
        tools = tree.root.add("Tools", expand=True)
        for item in self.inspection.tools:
            tools.add_leaf(f"{item.name} · {item.inclusion}", item)
        messages = tree.root.add("Messages", expand=True)
        for item in self.inspection.messages:
            messages.add_leaf(f"{item.label} · {item.inclusion}", item)
        summary = tree.root.add("Dropped-history summary", expand=True)
        if self.inspection.dropped_summary is None:
            summary.add_leaf("None · no earlier Entries represented")
        else:
            item = self.inspection.dropped_summary
            summary.add_leaf(f"Generated summary · {item.inclusion}", item)
        return tree

    def _first_content_detail(self) -> str:
        return (
            self._content_detail(self.inspection.instructions[0])
            if self.inspection.instructions
            else "Select a Tool, message, or summary to inspect its complete content."
        )

    def on_tree_node_highlighted(
        self,
        event: Tree.NodeHighlighted[ContextContentItem],
    ) -> None:
        if event.node.data is not None:
            self.query_one("#context-content-detail", Static).update(
                self._content_detail(event.node.data)
            )

    def on_tree_node_selected(
        self,
        event: Tree.NodeSelected[ContextContentItem],
    ) -> None:
        if event.node.data is not None:
            self.query_one("#context-content-detail", Static).update(
                self._content_detail(event.node.data)
            )

    def _overview_text(self) -> str:
        inspection = self.inspection
        projection = inspection.projection
        estimate = inspection.estimate
        anchor = (
            f"{inspection.provider_anchor_prompt_tokens} tokens (used to inform this estimate)"
            if inspection.provider_anchor_prompt_tokens is not None
            else "unavailable or not applicable"
        )
        effective = (
            f"{inspection.effective_input_limit} tokens"
            if inspection.effective_input_limit is not None
            else "unknown"
        )
        safe = (
            f"{inspection.safe_prompt_limit} tokens"
            if inspection.safe_prompt_limit is not None
            else "unknown"
        )
        utilization = (
            f"{inspection.utilization_percent:.1f}% (estimated)"
            if inspection.utilization_percent is not None
            else "unavailable because the effective input limit is unknown"
        )
        summary = (
            f"included, {inspection.dropped_summary.characters} characters"
            if inspection.dropped_summary is not None
            else "not present"
        )
        diagnostics = (
            "\n".join(f"- {item}" for item in inspection.diagnostics)
            if inspection.diagnostics
            else "- none"
        )
        return (
            "Inspection snapshot\n"
            "The selected Conversation View was projected at inspection time. Unsent drafts, "
            "queued messages, and future input are not Session Entries and are not included.\n\n"
            f"Model: {inspection.model_id or 'unresolved'}\n\n"
            "Projection\n"
            f"Session path: {projection.session_path_entries} Entries\n"
            "  ↓ materialize the selected durable branch\n"
            f"Conversation View: {projection.conversation_view_entries} Entries\n"
            "  ↓ select finite history; Compaction represents older Entries without deleting them\n"
            f"Context: {projection.context_messages} selected messages\n"
            "  ↓ inject stable instructions and any generated dropped-history summary\n"
            f"Model request: {projection.request_messages} messages\n\n"
            "Major Model-input sources\n"
            f"Stable instructions: {len(inspection.instructions)} origin groups, "
            f"{inspection.character_counts['system_prompt']} characters\n"
            f"Tools: {len(inspection.tools)} registered definitions, "
            f"{inspection.character_counts['tools']} characters\n"
            f"Selected messages: {len(inspection.messages)}, "
            f"{inspection.character_counts['messages']} characters\n"
            f"Generated dropped-history summary: {summary}\n\n"
            "Capacity and estimate provenance\n"
            f"Final Token Estimate: ~{estimate.tokens} tokens\n"
            f"Local Token Estimate: ~{estimate.local_tokens} tokens\n"
            f"Provider anchor contributed: {'yes' if estimate.used_provider_anchor else 'no'}\n"
            f"Latest applicable prompt Usage anchor: {anchor}\n"
            f"Effective input limit: {effective}\n"
            f"Safe prompt limit: {safe}\n"
            f"Utilization: {utilization}\n\n"
            "Diagnostics\n"
            f"{diagnostics}"
        )

    def _raw_request_text(self) -> str:
        request = self.inspection.request
        document = {
            "messages": request.messages,
            "tools": request.tools,
            "model": request.model,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        return "Exact immutable normalized Model request snapshot\n\n" + json.dumps(
            document, ensure_ascii=False, indent=2, sort_keys=True
        )

    @staticmethod
    def _content_detail(item: ContextContentItem) -> str:
        if isinstance(item, InstructionSection):
            return (
                f"Instruction origin: {item.origin}\n"
                f"Source: {item.source}\n"
                f"Inclusion: {item.inclusion}\n"
                f"Characters: {item.characters}\n\n"
                f"{item.content}"
            )
        if isinstance(item, InspectedTool):
            return (
                f"Tool: {item.name}\n"
                f"Provenance: {item.provenance}\n"
                f"Inclusion: {item.inclusion}\n"
                f"Characters: {item.characters}\n"
                f"Description: {item.description}\n\n"
                "Complete schema\n"
                f"{json.dumps(item.schema, ensure_ascii=False, indent=2, sort_keys=True)}"
            )
        if isinstance(item, InspectedMessage):
            return (
                f"{item.label}\n"
                f"Provenance: {item.provenance}\n"
                f"Inclusion: {item.inclusion}\n"
                f"Characters: {item.characters}\n\n"
                "Readable content\n"
                f"{item.readable_content}\n\n"
                "Complete Model-visible message\n"
                f"{json.dumps(item.message, ensure_ascii=False, indent=2, sort_keys=True)}"
            )
        return (
            "Generated dropped-history summary\n"
            f"Provenance: {item.provenance}\n"
            f"Inclusion: {item.inclusion}\n"
            f"Characters: {item.characters}\n\n"
            f"{item.content}"
        )

    def action_close(self) -> None:
        self.dismiss(None)
