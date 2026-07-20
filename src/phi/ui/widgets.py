"""Textual Host 使用的展示组件与输入消息类型。

本模块只负责把 Session Entry 和 Run Event 投影成界面元素；它不构造 Context、
不执行 Tool，也不参与 Harness 的停止决策。
"""

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

from phi.model import ContentDelta, ModelEvent, ModelResponse, ReasoningDelta, ToolCall, ToolResult
from phi.sessions import redact_text


class QueueDisposition(StrEnum):
    """描述 Run 进行期间暂存消息的后续处理方式。"""

    QUEUE = "queue"
    STEER = "steer"


@dataclass
class PendingMessage:
    """保存尚未成为 Session Entry 的用户输入。"""

    id: str
    text: str
    disposition: QueueDisposition = QueueDisposition.QUEUE
    target_run_generation: int | None = None


class PromptInput(TextArea):
    """提供 Enter 提交、Shift+Enter 换行的多行请求编辑器。"""

    MIN_HEIGHT = 3
    MAX_HEIGHT = 8

    class Submitted(Message):
        """通知父组件：用户已提交一段输入。"""

        def __init__(self, text: str) -> None:
            """记录要交给 Host 路由的原始输入。"""

            super().__init__()
            self.text = text

    class CompletionRequested(Message):
        """请求父组件尝试补全当前斜杠命令。"""

        def __init__(self, prompt: PromptInput) -> None:
            """保留发起请求的编辑器，供 Host 原位修改文本。"""

            super().__init__()
            self.prompt = prompt

    BINDINGS = [
        Binding("tab", "request_completion", priority=True),
        Binding("shift+enter", "newline", priority=True),
        Binding("enter", "submit", priority=True),
    ]

    def action_request_completion(self) -> None:
        """把 Tab 转换为可冒泡的补全请求。"""

        self.post_message(self.CompletionRequested(self))

    def action_newline(self) -> None:
        """在当前选区插入硬换行。"""

        self.insert("\n", maintain_selection_offset=False)

    def action_submit(self) -> None:
        """清空编辑器并把提交前的完整文本发送给父组件。"""

        # 先保存文本再清空，确保消息携带的是用户提交瞬间的草稿。
        text = self.text
        self.load_text("")
        self.post_message(self.Submitted(text))

    def resize_for_content(self) -> None:
        """随折行后的内容增长，同时为 Transcript 保留可用空间。"""

        # virtual_size 包含软换行后的实际高度，比简单统计换行符更准确。
        content_height = max(1, self.virtual_size.height)
        self.styles.height = min(
            max(content_height + 2, self.MIN_HEIGHT),
            self.MAX_HEIGHT,
        )


class TranscriptView(VerticalScroll):
    """滚动展示持久化 Entry 与实时 Run Event 的容器。"""


class UserMessageView(Static):
    """以纯文本呈现一条用户消息 Entry。"""

    def __init__(self, content: str) -> None:
        """创建禁用 Rich markup 的用户消息卡片。"""

        super().__init__(content, classes="user-message", markup=False)


class AssistantMessageView(Markdown):
    """呈现并增量更新一个 Model Step 的可见 Markdown 内容。"""

    def __init__(self, content: str = "") -> None:
        """以初始内容创建视图；空内容在收到流式增量前保持隐藏。"""

        self._content = content
        super().__init__(self._source(), classes="assistant-message")
        self.display = bool(content)

    def _source(self) -> str:
        """返回 Markdown 组件当前应渲染的完整源文本。"""

        return self._content

    def set_content(self, content: str) -> None:
        """用规范化的完整响应替换当前 Markdown 内容。"""

        self._content = content
        self.update(self._source())
        self.display = bool(content)

    def append_content(self, content: str) -> None:
        """把一个 ContentDelta 追加到已显示内容。"""

        self.set_content(f"{self._content}{content}")

    @property
    def is_empty(self) -> bool:
        """报告视图是否仍没有任何可见内容。"""

        return not self._content


class ReasoningView(Collapsible):
    """以默认折叠的 disclosure 呈现 Model reasoning。"""

    DEFAULT_CSS = """
    ReasoningView {
        width: 1fr;
        height: auto;
        margin: 0;
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
        margin: 0 0 0 1;
        padding: 0 0 0 1;
        border-left: solid $secondary;
    }
    ReasoningView .reasoning-content {
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(self, content: str = "") -> None:
        """创建 reasoning 正文并保留用户可控制的折叠状态。"""

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
        """替换 reasoning 正文，但不改变用户选择的折叠状态。"""

        self._content = content
        self._body.update(content)
        self.display = bool(content)

    def append_content(self, content: str) -> None:
        """追加 ReasoningDelta，并用 Thinking 标识尚在流式生成。"""

        self.title = "Thinking…"
        self.set_content(f"{self._content}{content}")

    def complete(self, content: str | None = None) -> None:
        """用最终响应校准正文，并把标题恢复为完成态。"""

        if content is not None:
            self.set_content(content)
        self.title = "Reasoning"

    @property
    def is_empty(self) -> bool:
        """报告是否没有收到 reasoning 内容。"""

        return not self._content


class ToolCallView(Vertical):
    """原位呈现一个 Tool Call 从执行中到结束的生命周期。"""

    def __init__(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        """创建含参数、加载指示器和运行态样式的 Tool 卡片。"""

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
        """生成 Tool Call 尚未返回结果时的文本。"""

        return f"Tool · {self.tool_name} · running\nArguments: {self.arguments}"

    def complete(self, output: str, error: str | None) -> None:
        """停止加载动画，并根据 Tool Result 更新样式和正文。"""

        # Tool 执行结果复用同一张卡片，避免把一次调用误读成多个操作。
        self.remove_class("running")
        self.add_class("failed" if error is not None else "completed")
        self._progress.display = False
        outcome = f"Error: {redact_text(error)}" if error is not None else f"Result: {output}"
        self._content.update(
            f"Tool · {self.tool_name} · complete\nArguments: {self.arguments}\n{outcome}"
        )


class ModelStepView(Vertical):
    """组合展示一个 Model Step 的 reasoning、正文和 Tool Call。"""

    DEFAULT_CSS = """
    ModelStepView {
        width: 1fr;
        height: auto;
        margin: 0;
        padding: 0;
        background: transparent;
    }
    """

    def __init__(
        self,
        *,
        content: str | None = None,
        reasoning: str | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> None:
        """从持久化响应或实时 Step 占位符构造视图。"""

        self._reasoning = ReasoningView(reasoning or "")
        self._assistant = AssistantMessageView(content or "")
        self._tool_views = {
            call.id: ToolCallView(call.id, call.name, call.arguments) for call in tool_calls
        }
        # 实时 Step 先挂载空子组件，后续 Event 才能直接在原位追加内容。
        live_placeholder = content is None and reasoning is None and not tool_calls
        children = []
        if reasoning or live_placeholder:
            children.append(self._reasoning)
        if content or live_placeholder:
            children.append(self._assistant)
        children.extend(self._tool_views.values())
        super().__init__(
            *children,
            classes="model-step",
        )

    def apply_delta(self, delta: ModelEvent) -> None:
        """把流式 Model Event 分派到正文或 reasoning 子视图。"""

        if isinstance(delta, ContentDelta):
            self._assistant.append_content(delta.text)
        elif isinstance(delta, ReasoningDelta):
            self._reasoning.append_content(delta.text)

    async def complete_response(self, response: ModelResponse) -> None:
        """用规范化最终响应校准流式内容并清理空占位符。"""

        if response.content is not None:
            self._assistant.set_content(response.content)
        self._reasoning.complete(response.reasoning)
        await self._remove_empty_placeholders()

    async def start_tool(self, call: ToolCall) -> None:
        """在当前 Step 内挂载新开始执行的 Tool Call。"""

        view = ToolCallView(call.id, call.name, call.arguments)
        self._tool_views[call.id] = view
        await self.mount(view)

    def complete_tool(self, result: ToolResult) -> None:
        """按 call_id 将 Tool Result 路由到对应卡片。"""

        view = self._tool_views.get(result.call_id)
        if view is not None:
            view.complete(result.output, result.error)

    async def finish(self) -> None:
        """在 Run 终止时结束未收尾的展示状态。"""

        self._reasoning.complete()
        await self._remove_empty_placeholders()

    async def _remove_empty_placeholders(self) -> None:
        """删除实时 Step 为等待首个增量而创建的空子组件。"""

        if self._reasoning.is_empty and self._reasoning.is_mounted:
            await self._reasoning.remove()
        if self._assistant.is_empty and self._assistant.is_mounted:
            await self._assistant.remove()

    @property
    def tool_call_ids(self) -> tuple[str, ...]:
        """返回本 Step 已知的 Tool Call 标识。"""

        return tuple(self._tool_views)

    @property
    def is_empty(self) -> bool:
        """报告该 Step 是否没有任何可展示内容。"""

        return self._reasoning.is_empty and self._assistant.is_empty and not self._tool_views


class CompactionEntryView(Static):
    """呈现 Session 中持久化的 Compaction 结构标记。"""

    def __init__(self, summary: str) -> None:
        """显示被压缩历史的摘要。"""

        super().__init__(
            f"Compaction\n{summary}",
            classes="compaction-entry",
            markup=False,
        )


class RunStatusView(Static):
    """呈现不能用低强调边界表达的 Run 状态或诊断。"""

    def __init__(self, text: str, *, failed: bool = False) -> None:
        """按是否失败选择普通或错误样式。"""

        super().__init__(
            text,
            classes=f"run-status{' failed' if failed else ''}",
            markup=False,
        )


class RunBoundaryView(Static):
    """在一个 Run 终态后呈现低强调的视觉边界。"""

    DEFAULT_CSS = """
    RunBoundaryView {
        width: 1fr;
        height: 1;
        margin: 0 1;
        color: $text-muted;
        text-style: dim;
    }
    RunBoundaryView.warning {
        color: $warning;
        text-style: none;
    }
    """

    def __init__(self, finished_at: datetime, *, status_label: str | None = None) -> None:
        """使用本地时间和可选终态标签构造分隔线标题。"""

        local_finished_at = finished_at.astimezone()
        self.time_label = local_finished_at.strftime("%H:%M")
        self.status_label = status_label
        self.boundary_title = (
            self.time_label if status_label is None else f"{status_label} · {self.time_label}"
        )
        classes = "run-boundary completed" if status_label is None else "run-boundary warning"
        super().__init__("", classes=classes, markup=False)

    def render(self) -> Rule:
        """使用组件当前 Rich 样式绘制水平分隔线。"""

        style = self.rich_style
        return Rule(
            Text(self.boundary_title, style=style),
            characters="─",
            style=style,
        )


class QueueMessageRow(Horizontal):
    """呈现一条可编辑、切换 Steer 或移除的暂存输入。"""

    def __init__(self, pending: PendingMessage) -> None:
        """为 PendingMessage 创建一行文本预览与操作按钮。"""

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
        """生成折叠换行后的单行 Queue/Steer 预览。"""

        disposition = "Queue" if pending.disposition is QueueDisposition.QUEUE else "Steer"
        preview = " ".join(pending.text.split())
        return f"{disposition} · {preview}"

    def refresh_pending(self, pending: PendingMessage) -> None:
        """在切换 disposition 后原位刷新标签和按钮文案。"""

        self.query_one(f"#queued-text-{pending.id}", Static).update(self._label(pending))
        toggle = self.query_one(f"#toggle-{pending.id}", Button)
        toggle.label = "Queue" if pending.disposition is QueueDisposition.STEER else "Steer"
