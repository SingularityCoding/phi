"""Textual Host 的模态交互与只读 Context 检查界面。

这些 Screen 只收集选择或展示应用服务已经准备好的数据。尤其是 Context 检查器
只读取不可变快照，不会调用 Model、追加 Session Entry 或触发 Compaction。
"""

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
    """为 Host 操作提供确定、可用键盘访问的单选模态框。"""

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
        """保存标题和 ``(显示标签, 返回值)`` 选项。"""

        super().__init__()
        self.selection_title = title
        self.options = options

    def compose(self) -> ComposeResult:
        """按输入顺序生成可滚动的选项按钮。"""

        with Vertical(id="selection-dialog"):
            yield Static(self.selection_title, classes="screen-title", markup=False)
            with VerticalScroll(id="selection-options"):
                for index, (label, _value) in enumerate(self.options):
                    yield Button(Text(label), id=f"selection-{index}")

    def action_cancel(self) -> None:
        """用 ``None`` 结束等待，表示用户取消选择。"""

        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """把按钮索引还原成调用方需要的稳定选项值。"""

        button_id = event.button.id
        if button_id is None or not button_id.startswith("selection-"):
            return
        index = int(button_id.removeprefix("selection-"))
        self.dismiss(self.options[index][1])


class PromptArgumentsScreen(ModalScreen[dict[str, str] | None]):
    """在读取一个 MCP Prompt 前收集其声明的字符串参数。"""

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
        """保存 MCP Prompt 定义和命令行中已提供的初值。"""

        super().__init__()
        self.prompt = prompt
        self.initial = initial

    def compose(self) -> ComposeResult:
        """为每个 MCP Prompt 参数生成带说明的输入框。"""

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
        """不提交参数并关闭模态框。"""

        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """校验必填参数，成功后返回按名称组织的值。"""

        if event.button.id == "prompt-arguments-cancel":
            self.dismiss(None)
            return
        if event.button.id != "prompt-arguments-submit":
            return
        # 输入框序号与 prompt.arguments 的稳定顺序一一对应。
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
    """为只读 Host 命令展示可用键盘关闭的详情。"""

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
        """保存要以纯文本展示的标题和正文。"""

        super().__init__()
        self.info_title = title
        self.content = content

    def compose(self) -> ComposeResult:
        """生成详情正文和显式关闭按钮。"""

        with Vertical(id="info-dialog"):
            yield Static(self.info_title, classes="screen-title", markup=False)
            yield Static(self.content, id="info-content", markup=False)
            yield Button("Close", id="info-close", variant="primary")

    def action_close(self) -> None:
        """关闭详情 Screen。"""

        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """响应显式关闭按钮。"""

        if event.button.id == "info-close":
            self.dismiss(None)


class ApprovalPromptScreen(ModalScreen[AskResolution]):
    """为一个待审批 Tool Call 提供默认拒绝的人类决策边界。"""

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
        """保存 Model 提议的 Tool Call 及其注册表定义。"""

        super().__init__()
        self.call = call
        self.tool = tool

    def compose(self) -> ComposeResult:
        """展示 Tool、审批类别、参数和三个明确决策。"""

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
        """在 Escape 等退出路径上返回拒绝，确保 fail-closed。"""

        self.dismiss(AskResolution.DENY)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """把按钮映射为审批解析结果；未知按钮同样按拒绝处理。"""

        # get 的默认值刻意设为 DENY：界面标识异常不能扩大 Tool 执行权限。
        decisions = {
            "approval-once": AskResolution.ALLOW_ONCE,
            "approval-session": AskResolution.ALLOW_FOR_SESSION,
            "approval-deny": AskResolution.DENY,
        }
        self.dismiss(decisions.get(event.button.id, AskResolution.DENY))


type ContextContentItem = InstructionSection | InspectedTool | InspectedMessage | InspectedSummary


class ContextContentsTree(Tree[ContextContentItem]):
    """以可导航树展示组成 Model 输入的各类内容。"""

    def key_1(self) -> None:
        """从树获得焦点时仍允许快捷键 1 切到 Overview。"""

        explorer = self._explorer()
        self.call_after_refresh(explorer.action_show_overview)

    def key_2(self) -> None:
        """从树获得焦点时响应快捷键 2 并留在 Contents。"""

        explorer = self._explorer()
        self.call_after_refresh(explorer.action_show_contents)

    def key_3(self) -> None:
        """从树获得焦点时仍允许快捷键 3 切到 Raw request。"""

        explorer = self._explorer()
        self.call_after_refresh(explorer.action_show_raw)

    def _explorer(self) -> ContextInspectorScreen:
        """定位拥有此树的 Context 检查 Screen。"""

        return self.query_ancestor(ContextInspectorScreen)


class ContextOverview(VerticalScroll):
    """概览一次不可变 Context 检查的关键投影与容量信息。"""

    def __init__(self, inspection: ContextInspection) -> None:
        """保存应用服务生成的只读检查快照。"""

        super().__init__(id="context-overview-scroll")
        self.inspection = inspection

    def compose(self) -> ComposeResult:
        """生成请求投影、输入来源、容量来源和诊断概览。"""

        # 先提取频繁使用的快照字段，让下面的布局代码保持“数据到视图”的顺序。
        inspection = self.inspection
        projection = inspection.projection
        estimate = inspection.estimate
        effective_limit = inspection.effective_input_limit
        utilization = inspection.utilization_percent

        # 顶部明确快照边界，防止学生把未发送草稿误认为 Context 的一部分。
        yield Static(
            "Snapshot of the selected Conversation View. Unsent drafts, queued messages, and "
            "future input are not included.",
            id="context-overview-intro",
            markup=False,
        )

        # 四个并列指标只呈现快照事实；输入上限未知时绝不虚构利用率。
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

        # 这条投影链强调 Session、Conversation View、Context 与请求并非同一对象。
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

        # 来源面板解释“放进了什么”，容量面板解释“估算依据是什么”。
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

        # 诊断使用语义样式，但仍保持只读，不据此修改 Context。
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
        """同时生成宽屏横向箭头和窄屏纵向箭头。"""

        return (
            Static("→", classes="context-projection-arrow context-projection-arrow-wide"),
            Static("↓", classes="context-projection-arrow context-projection-arrow-narrow"),
        )

    def _source_details(self) -> str:
        """汇总稳定指令、Tool、消息和历史摘要的字符计数。"""

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
        """解释 Token Estimate、Usage 锚点与安全输入上限的来源。"""

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
    """全屏只读检查一个不可变的 Model 请求快照。"""

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
        """保存快照并预先建立 Tool Result 所需的调用名称索引。"""

        super().__init__()
        self.inspection = inspection
        self._tool_names_by_call_id = self._index_tool_call_names(inspection.messages)
        self._first_content_node = None

    def compose(self) -> ComposeResult:
        """构造 Overview、Contents 和 Raw request 三个固定视图。"""

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
        """首次挂载时根据终端宽度选择横向或纵向布局。"""

        self.set_class(self.size.width < 80, "narrow")

    def on_resize(self) -> None:
        """终端尺寸变化时同步窄屏语义样式类。"""

        self.set_class(self.size.width < 80, "narrow")

    def action_show_overview(self) -> None:
        """切换到 Overview，并清除 Contents 树的焦点。"""

        views = self.query_one("#context-views", TabbedContent)
        self.set_focus(None)
        views.active = "context-overview"

    def action_show_contents(self) -> None:
        """切换到 Contents，并把键盘焦点放到首个内容节点。"""

        self.query_one("#context-views", TabbedContent).active = "context-contents"
        tree = self.query_one("#context-contents-tree", Tree)
        tree.focus()
        if self._first_content_node is not None:
            tree.select_node(self._first_content_node)

    def action_show_raw(self) -> None:
        """切换到精确的规范化 ModelRequest 展示。"""

        views = self.query_one("#context-views", TabbedContent)
        self.set_focus(None)
        views.active = "context-raw"

    def _contents_tree(self) -> Tree[ContextContentItem]:
        """按指令、Tool、消息和 Compaction 摘要构建内容树。"""

        # Tree 节点的 data 直接携带检查对象，选择事件无需按标签反查数据。
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
        # Tool 很多时默认折叠，避免打开检查器后挤走更常阅读的消息分组。
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
        # Compaction 摘要是 Context 输入来源之一，但不是普通对话消息。
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
        """生成带弱化数量标记的树分组标题。"""

        return Text.assemble((title, "bold"), (f"  {count}", "dim"))

    @staticmethod
    def _index_tool_call_names(messages: tuple[InspectedMessage, ...]) -> dict[str, str]:
        """从 assistant 消息索引 call_id，供 Tool Result 显示语义名称。"""

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
        """把 Model 可见角色映射为树节点的符号、标题、后缀和样式。"""

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
        """从不可信形状的 assistant 消息中安全提取 Tool 名称。"""

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
        """生成带顺序号和语义角色的消息树标签。"""

        symbol, title, suffix, style = self._message_identity(item)
        return Text.assemble(
            (f"{item.index:02d}  ", "dim"),
            (f"{symbol} {title}", style),
            (suffix, "dim"),
        )

    def _first_content_detail(self) -> str:
        """为详情面板选择稳定的初始内容。"""

        return (
            self._content_detail(self.inspection.instructions[0])
            if self.inspection.instructions
            else "Select a Tool, message, or summary to inspect its complete content."
        )

    def on_tree_node_highlighted(
        self,
        event: Tree.NodeHighlighted[ContextContentItem],
    ) -> None:
        """键盘移动高亮时立即更新完整详情。"""

        if event.node.data is not None:
            self.query_one("#context-content-detail", Static).update(
                self._content_detail(event.node.data)
            )

    def on_tree_node_selected(
        self,
        event: Tree.NodeSelected[ContextContentItem],
    ) -> None:
        """鼠标或 Enter 选中节点时更新完整详情。"""

        if event.node.data is not None:
            self.query_one("#context-content-detail", Static).update(
                self._content_detail(event.node.data)
            )

    def _raw_request_text(self) -> str:
        """序列化冻结快照中的精确规范化 Model 请求字段。"""

        # 显式枚举请求字段，避免把展示逻辑绑定到对象的内部序列化方式。
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
        """按检查项类型生成带来源、纳入状态和完整内容的详情。"""

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
        """关闭只读检查器并返回主 Host。"""

        self.dismiss(None)
