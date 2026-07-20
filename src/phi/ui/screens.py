from __future__ import annotations

import json
from collections.abc import Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    ProgressBar,
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


class ContextOverview(VerticalScroll):
    """Scannable visual summary of one immutable Context inspection."""

    def __init__(self, inspection: ContextInspection) -> None:
        super().__init__(id="context-overview-scroll")
        self.inspection = inspection

    def compose(self) -> ComposeResult:
        inspection = self.inspection
        projection = inspection.projection
        estimate = inspection.estimate
        effective_limit = inspection.effective_input_limit
        utilization = inspection.utilization_percent

        yield Static(
            "Snapshot of the selected Conversation View. Unsent drafts, queued messages, and "
            "future input are not included.",
            id="context-overview-intro",
            markup=False,
        )

        with Grid(id="context-overview-metrics"):
            with Vertical(classes="context-overview-metric"):
                yield Static("MODEL", classes="context-overview-label", markup=False)
                yield Static(
                    inspection.model_id or "unresolved",
                    id="context-model-value",
                    classes="context-overview-value",
                    markup=False,
                )
            with Vertical(classes="context-overview-metric"):
                yield Static("TOKEN ESTIMATE", classes="context-overview-label", markup=False)
                yield Static(
                    f"~{estimate.tokens}",
                    id="context-estimate-value",
                    classes="context-overview-value",
                    markup=False,
                )
            with Vertical(classes="context-overview-metric"):
                yield Static("INPUT LIMIT", classes="context-overview-label", markup=False)
                yield Static(
                    str(effective_limit) if effective_limit is not None else "unknown",
                    id="context-limit-value",
                    classes="context-overview-value",
                    markup=False,
                )
            with Vertical(classes="context-overview-metric"):
                yield Static("UTILIZATION", classes="context-overview-label", markup=False)
                yield Static(
                    f"{utilization:.1f}%" if utilization is not None else "unavailable",
                    id="context-utilization-value",
                    classes="context-overview-value",
                    markup=False,
                )

        yield Static("Request projection", classes="context-overview-section-title", markup=False)
        with Horizontal(id="context-projection-flow"):
            with Vertical(
                id="context-projection-session",
                classes="context-projection-stage",
            ):
                yield Static("SESSION PATH", classes="context-overview-label", markup=False)
                yield Static(
                    f"{projection.session_path_entries} Entries",
                    id="context-projection-session-value",
                    classes="context-projection-value",
                    markup=False,
                )
                yield Static(
                    "selected durable branch",
                    classes="context-projection-note",
                    markup=False,
                )
            yield from self._projection_arrow()
            with Vertical(id="context-projection-view", classes="context-projection-stage"):
                yield Static("CONVERSATION VIEW", classes="context-overview-label", markup=False)
                yield Static(
                    f"{projection.conversation_view_entries} Entries",
                    id="context-projection-view-value",
                    classes="context-projection-value",
                    markup=False,
                )
                yield Static(
                    "materialized history",
                    classes="context-projection-note",
                    markup=False,
                )
            yield from self._projection_arrow()
            with Vertical(id="context-projection-context", classes="context-projection-stage"):
                yield Static("FINITE CONTEXT", classes="context-overview-label", markup=False)
                yield Static(
                    f"{projection.context_messages} messages",
                    id="context-projection-context-value",
                    classes="context-projection-value",
                    markup=False,
                )
                yield Static("budgeted selection", classes="context-projection-note", markup=False)
            yield from self._projection_arrow()
            with Vertical(id="context-projection-request", classes="context-projection-stage"):
                yield Static("MODEL REQUEST", classes="context-overview-label", markup=False)
                yield Static(
                    f"{projection.request_messages} messages",
                    id="context-projection-request-value",
                    classes="context-projection-value",
                    markup=False,
                )
                yield Static("normalized payload", classes="context-projection-note", markup=False)

        yield Static("What contributes to this request", classes="context-overview-section-title")
        with Grid(id="context-overview-details"):
            with Vertical(classes="context-overview-panel", id="context-sources-panel"):
                yield Static("Model input", classes="context-overview-panel-title", markup=False)
                yield Static(
                    self._source_details(),
                    id="context-source-details",
                    classes="context-overview-details",
                    markup=False,
                )
            with Vertical(classes="context-overview-panel", id="context-capacity-panel"):
                yield Static("Capacity & provenance", classes="context-overview-panel-title")
                yield Static(
                    self._capacity_details(),
                    id="context-capacity-details",
                    classes="context-overview-details",
                    markup=False,
                )
                if effective_limit is None:
                    yield Static(
                        "Capacity percentage unavailable until the Model input limit is known.",
                        id="context-capacity-unknown",
                        classes="context-capacity-note",
                        markup=False,
                    )
                else:
                    yield Static(
                        "Estimated use of effective input limit",
                        classes="context-capacity-note",
                        markup=False,
                    )
                    bar = ProgressBar(
                        total=effective_limit,
                        show_eta=False,
                        id="context-capacity-bar",
                    )
                    bar.update(progress=estimate.tokens)
                    yield bar

        diagnostic_class = "has-diagnostics" if inspection.diagnostics else "is-clear"
        with Vertical(
            id="context-diagnostics-panel",
            classes=f"context-overview-panel {diagnostic_class}",
        ):
            yield Static("Diagnostics", classes="context-overview-panel-title", markup=False)
            yield Static(
                "\n".join(f"• {item}" for item in inspection.diagnostics)
                if inspection.diagnostics
                else "No Context diagnostics for this snapshot.",
                id="context-diagnostics-details",
                classes="context-overview-details",
                markup=False,
            )

    @staticmethod
    def _projection_arrow() -> tuple[Static, Static]:
        return (
            Static("→", classes="context-projection-arrow context-projection-arrow-wide"),
            Static("↓", classes="context-projection-arrow context-projection-arrow-narrow"),
        )

    def _source_details(self) -> str:
        inspection = self.inspection
        summary = (
            f"included · {inspection.dropped_summary.characters} characters"
            if inspection.dropped_summary is not None
            else "not present"
        )
        return (
            f"Stable instructions: {len(inspection.instructions)} origins · "
            f"{inspection.character_counts['system_prompt']} characters\n"
            f"Tools: {len(inspection.tools)} definitions · "
            f"{inspection.character_counts['tools']} characters\n"
            f"Selected messages: {len(inspection.messages)} · "
            f"{inspection.character_counts['messages']} characters\n"
            f"Dropped-history summary: {summary}"
        )

    def _capacity_details(self) -> str:
        inspection = self.inspection
        estimate = inspection.estimate
        anchor = (
            f"{inspection.provider_anchor_prompt_tokens} tokens"
            if inspection.provider_anchor_prompt_tokens is not None
            else "unavailable or not applicable"
        )
        safe = (
            f"{inspection.safe_prompt_limit} tokens"
            if inspection.safe_prompt_limit is not None
            else "unknown"
        )
        return (
            f"Local estimate: ~{estimate.local_tokens} tokens\n"
            f"Provider anchor contributed: {'yes' if estimate.used_provider_anchor else 'no'}\n"
            f"Latest prompt Usage anchor: {anchor}\n"
            f"Safe prompt limit: {safe}"
        )


class ContextInspectorScreen(Screen[None]):
    """Full-screen read-only explorer for one immutable Model-request snapshot."""

    EXPANDED_TOOL_LIMIT = 6

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
    #context-overview-intro {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    #context-overview-metrics {
        height: 5;
        grid-size: 4 1;
        grid-columns: 1fr 1fr 1fr 1fr;
        grid-rows: 5;
        grid-gutter: 1;
        margin-bottom: 1;
    }
    .context-overview-metric {
        height: 5;
        padding: 0 1;
        border: round $accent;
        background: $panel;
        content-align: left middle;
    }
    .context-overview-label {
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    .context-overview-value, .context-projection-value {
        height: 1;
        text-style: bold;
        color: $accent;
    }
    .context-overview-section-title {
        height: 2;
        padding-top: 1;
        text-style: bold;
    }
    #context-projection-flow {
        height: 5;
        layout: horizontal;
    }
    .context-projection-stage {
        width: 1fr;
        height: 5;
        padding: 0 1;
        border: round $secondary;
        background: $panel;
        content-align: left middle;
    }
    .context-projection-note {
        height: 1;
        color: $text-muted;
    }
    .context-projection-arrow {
        width: 3;
        height: 5;
        content-align: center middle;
        color: $accent;
        text-style: bold;
    }
    .context-projection-arrow-narrow { display: none; }
    #context-overview-details {
        height: auto;
        grid-size: 2 1;
        grid-columns: 1fr 1fr;
        grid-rows: auto;
        grid-gutter: 1;
    }
    .context-overview-panel {
        height: auto;
        min-height: 8;
        padding: 0 1 1 1;
        border: round $secondary;
        background: $panel;
    }
    .context-overview-panel-title {
        height: 2;
        padding-top: 1;
        text-style: bold;
    }
    .context-overview-details, .context-capacity-note {
        height: auto;
    }
    .context-capacity-note {
        margin-top: 1;
        color: $text-muted;
    }
    #context-capacity-bar { margin-top: 1; }
    #context-diagnostics-panel {
        margin-top: 1;
        min-height: 5;
    }
    #context-diagnostics-panel.is-clear { border: round $success; }
    #context-diagnostics-panel.has-diagnostics { border: round $warning; }
    #context-raw-request, #context-content-detail {
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
    ContextInspectorScreen.narrow #context-overview-metrics {
        height: 11;
        grid-size: 2 2;
        grid-columns: 1fr 1fr;
        grid-rows: 5 5;
    }
    ContextInspectorScreen.narrow #context-projection-flow {
        height: auto;
        layout: vertical;
    }
    ContextInspectorScreen.narrow .context-projection-stage {
        width: 100%;
        height: 4;
    }
    ContextInspectorScreen.narrow .context-projection-arrow-wide { display: none; }
    ContextInspectorScreen.narrow .context-projection-arrow-narrow {
        display: block;
        width: 100%;
        height: 1;
    }
    ContextInspectorScreen.narrow #context-overview-details {
        grid-size: 1 2;
        grid-columns: 1fr;
        grid-rows: auto auto;
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
        self._tool_names_by_call_id = self._index_tool_call_names(inspection.messages)
        self._first_content_node = None

    def compose(self) -> ComposeResult:
        yield Static("Context request explorer", id="context-explorer-title", markup=False)
        with TabbedContent(initial="context-overview", id="context-views"):
            with TabPane("Overview", id="context-overview"):
                yield ContextOverview(self.inspection)
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
        instructions = tree.root.add(
            self._group_label("Instructions", len(self.inspection.instructions)),
            expand=True,
        )
        for section in self.inspection.instructions:
            node = instructions.add_leaf(
                Text.assemble(("◆ ", "cyan"), section.origin),
                section,
            )
            if self._first_content_node is None:
                self._first_content_node = node
        tools = tree.root.add(
            self._group_label("Tools", len(self.inspection.tools)),
            expand=len(self.inspection.tools) <= self.EXPANDED_TOOL_LIMIT,
        )
        for item in self.inspection.tools:
            tools.add_leaf(Text.assemble(("• ", "magenta"), item.name), item)
        messages = tree.root.add(
            self._group_label("Messages", len(self.inspection.messages)),
            expand=True,
        )
        for item in self.inspection.messages:
            messages.add_leaf(self._message_tree_label(item), item)
        if self.inspection.dropped_summary is not None:
            item = self.inspection.dropped_summary
            summary = tree.root.add(self._group_label("Compaction", 1), expand=True)
            summary.add_leaf(
                Text.assemble(("◆ ", "yellow"), "Dropped-history summary"),
                item,
            )
        return tree

    @staticmethod
    def _group_label(title: str, count: int) -> Text:
        return Text.assemble((title, "bold"), (f"  {count}", "dim"))

    @staticmethod
    def _index_tool_call_names(messages: tuple[InspectedMessage, ...]) -> dict[str, str]:
        names: dict[str, str] = {}
        for item in messages:
            tool_calls = item.message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, Mapping):
                    continue
                call_id = call.get("id")
                function = call.get("function")
                if not isinstance(call_id, str) or not isinstance(function, Mapping):
                    continue
                name = function.get("name")
                if isinstance(name, str):
                    names[call_id] = name
        return names

    def _message_identity(self, item: InspectedMessage) -> tuple[str, str, str, str]:
        role = item.message.get("role")
        if role == "user":
            return "●", "User", "", "green"
        if role == "assistant":
            tool_names = self._assistant_tool_names(item.message)
            suffix = f" · calls {', '.join(tool_names)}" if tool_names else ""
            return "◆", "Assistant", suffix, "cyan"
        if role == "tool":
            call_id = item.message.get("tool_call_id")
            tool_name = (
                self._tool_names_by_call_id.get(call_id) if isinstance(call_id, str) else None
            )
            suffix = f" · {tool_name}" if tool_name is not None else ""
            return "↳", "Tool result", suffix, "yellow"
        return "•", str(role or "Unknown").title(), "", "white"

    @staticmethod
    def _assistant_tool_names(message: Mapping[str, object]) -> tuple[str, ...]:
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            return ()
        names: list[str] = []
        for call in tool_calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            if isinstance(name, str):
                names.append(name)
        return tuple(names)

    def _message_tree_label(self, item: InspectedMessage) -> Text:
        symbol, title, suffix, style = self._message_identity(item)
        return Text.assemble(
            (f"{item.index:02d}  ", "dim"),
            (f"{symbol} {title}", style),
            (suffix, "dim"),
        )

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

    def _content_detail(self, item: ContextContentItem) -> str:
        if isinstance(item, InstructionSection):
            return (
                f"Instructions / {item.origin}\n\n"
                f"Source: {item.source}\n"
                f"Inclusion: {item.inclusion}\n"
                f"Characters: {item.characters}\n\n"
                f"{item.content}"
            )
        if isinstance(item, InspectedTool):
            return (
                f"Tools / {item.name}\n\n"
                f"Provenance: {item.provenance}\n"
                f"Inclusion: {item.inclusion}\n"
                f"Characters: {item.characters}\n"
                f"Description: {item.description}\n\n"
                "Complete schema\n"
                f"{json.dumps(item.schema, ensure_ascii=False, indent=2, sort_keys=True)}"
            )
        if isinstance(item, InspectedMessage):
            _symbol, title, suffix, _style = self._message_identity(item)
            kind = item.label.rsplit(" ", maxsplit=1)[0]
            return (
                f"Messages / {item.index:02d} {title}{suffix}\n\n"
                f"Type: {kind}\n"
                f"Provenance: {item.provenance}\n"
                f"Inclusion: {item.inclusion}\n"
                f"Characters: {item.characters}\n\n"
                "Readable content\n"
                f"{item.readable_content}\n\n"
                "Complete Model-visible message\n"
                f"{json.dumps(item.message, ensure_ascii=False, indent=2, sort_keys=True)}"
            )
        return (
            "Compaction / Dropped-history summary\n\n"
            f"Provenance: {item.provenance}\n"
            f"Inclusion: {item.inclusion}\n"
            f"Characters: {item.characters}\n\n"
            f"{item.content}"
        )

    def action_close(self) -> None:
        self.dismiss(None)
