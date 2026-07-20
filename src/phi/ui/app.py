"""Phi 的 Textual Host：路由交互并观察共享应用服务。

此模块拥有当前不可变 ``SessionHandle`` 和 cwd 作用域运行时，但仍是薄 Host：
Context 构造、Session 持久化、Tool 调度和 Run 控制循环均由下层共享服务负责。
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import datetime
from os.path import commonprefix
from pathlib import Path
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static, TextArea

from phi.bootstrap import HostRuntime, build_interactive_runtime
from phi.cli.model_selection import resolve_available_model
from phi.harness import (
    Hooks,
    ModelCallCompleted,
    ModelCallDelta,
    ModelCallStarted,
    RunEvent,
    RunFinished,
    RunStarted,
    RunStatus,
    ToolCallCompleted,
    ToolCallStarted,
)
from phi.mcp import McpPrompt, McpPromptResult
from phi.model import ModelInfo, ToolCall
from phi.sessions import (
    AssistantMessageEntry,
    CompactionEntry,
    ContextInspection,
    SessionHandle,
    ToolResultEntry,
    UserMessageEntry,
    create_session,
    fork_session,
    inspect_context,
    list_leaves,
    list_session_handles,
    manual_compact,
    materialize_presentation,
    redact_text,
    rename_session,
    resume_session,
    select_model,
    send_message,
    switch_leaf,
)
from phi.tools import (
    ACCEPT_EDITS_MODE,
    BYPASS_MODE,
    DEFAULT_MODE,
    PLAN_MODE,
    AskResolution,
    Tool,
)
from phi.ui.screens import (
    ApprovalPromptScreen,
    ContextInspectorScreen,
    InfoScreen,
    PromptArgumentsScreen,
    SelectionScreen,
)
from phi.ui.widgets import (
    CompactionEntryView,
    ModelStepView,
    PendingMessage,
    PromptInput,
    QueueDisposition,
    QueueMessageRow,
    RunBoundaryView,
    RunStatusView,
    TranscriptView,
    UserMessageView,
)

type RuntimeFactory = Callable[[Path], Awaitable[HostRuntime]]
type Clock = Callable[[], datetime]


def _local_now() -> datetime:
    """返回带本地时区的当前时间，供 Run 结束边界展示。"""

    return datetime.now().astimezone()


BUILTIN_COMMANDS = (
    "/new",
    "/resume",
    "/fork",
    "/tree",
    "/session",
    "/name",
    "/context",
    "/mcp",
    "/model",
    "/compact",
    "/permissions",
    "/quit",
)


class PhiApp(App[None]):
    """在 cwd 作用域运行时和不可变 Session handle 上提供交互式 Host。"""

    CSS = """
    Screen { layout: vertical; }
    #status-bar {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #status-bar.context-warning { color: $warning; }
    #transcript { height: 1fr; padding: 0 1; }
    .user-message {
        height: auto;
        margin: 0;
        padding: 0 1;
        background: $boost;
    }
    .assistant-message {
        margin: 0;
        padding: 0 1;
        background: transparent;
    }
    .tool-call { margin: 1 2; padding: 1; border: round $accent; height: auto; }
    .tool-call-content { height: auto; }
    .tool-call-progress { height: 1; }
    .tool-call.failed, .run-status.failed { border: round $error; color: $error; }
    .compaction-entry { margin: 1 2; padding: 1; border: dashed $secondary; height: auto; }
    #queue { height: auto; max-height: 6; }
    #command-completion {
        height: auto;
        max-height: 6;
        margin: 0 1;
        padding: 0 1;
        background: $panel;
        border-left: solid $secondary;
        display: none;
    }
    .queue-row {
        height: 1;
        min-height: 1;
        padding: 0 1;
        background: $panel;
    }
    .queued-message {
        width: 1fr;
        height: 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .queue-row Button {
        width: auto;
        min-width: 0;
        height: 1;
        min-height: 1;
        margin: 0 0 0 1;
        padding: 0 1;
        border: none;
        background: transparent;
    }
    .queue-row Button:hover { background: $boost; }
    .queue-row Button:focus { background: $boost; text-style: bold; }
    #prompt { height: 3; border: round $accent; }
    #composer-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    BINDINGS = [
        ("escape", "cancel_run", "Cancel Run"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        initial_session: SessionHandle | None = None,
        cwd: Path | None = None,
        runtime_factory: RuntimeFactory | None = None,
        max_steps: int = 20,
        clock: Clock | None = None,
    ) -> None:
        """配置启动参数并初始化尚未挂载的 Host 状态。"""

        super().__init__()
        self.current_session = initial_session
        self.cwd = (cwd or Path.cwd()).expanduser().resolve()
        self._runtime_factory = runtime_factory
        self._max_steps = max_steps
        self._clock = clock or _local_now
        self._runtime: HostRuntime | None = None
        self._runtime_closed = False
        self._shutting_down = False
        # Run 生命周期与界面排队状态分开记录，便于在 RunStarted 前也接受取消请求。
        self._active_run_task: asyncio.Task[None] | None = None
        self._active_run_started = False
        self._cancel_requested = False
        self._run_generation = 0
        self._active_run_generation: int | None = None
        self._run_active = False
        self._draining = False
        self._session_operation_active = False
        # PendingMessage 仍是草稿；只有 send_message 真正发送后才成为 Session Entry。
        self._pending: list[PendingMessage] = []
        self._reported_diagnostics: set[str] = set()
        self._step_views: dict[tuple[str, int], ModelStepView] = {}
        self._mcp_prompts: dict[str, McpPrompt] = {}
        self._context_inspection: ContextInspection | None = None

    def compose(self) -> ComposeResult:
        """按照状态栏、Transcript、Queue、补全、Prompt 的顺序构造主界面。"""

        yield Static("Starting…", id="status-bar", markup=False)
        yield TranscriptView(id="transcript")
        yield Vertical(id="queue")
        yield Static("", id="command-completion")
        yield PromptInput(id="prompt", placeholder="Ask Phi…")
        yield Static("Starting…", id="composer-hint", markup=False)

    async def on_mount(self) -> None:
        """构建运行时、恢复 Session，并在首屏渲染只读状态。"""

        try:
            # cwd 级资源只构建一次；之后的 Session 切换复用同一个运行时。
            runtime = await self._build_runtime()
            self._runtime = runtime
            if runtime.approval_policy is not None:
                runtime.approval_policy.set_resolver(self._resolve_approval)
            self._mcp_prompts = {
                prompt.command: prompt for prompt in await runtime.resources.list_mcp_prompts()
            }
            # Host 始终持有服务返回的最新不可变 handle，而不在原对象上修改叶节点。
            if self.current_session is None:
                model_id, _ = resolve_available_model(
                    runtime.settings.default_model,
                    available_models=runtime.available_models,
                    missing_message="no Model was selected; configure PHI_DEFAULT_MODEL",
                )
                self.current_session = await create_session(runtime.storage, model=model_id)
            else:
                self.current_session = await resume_session(
                    runtime.storage,
                    self.current_session.session_id,
                )
            # 持久化对话先渲染，再附加启动诊断和 Context 容量状态。
            await self._render_current_conversation()
            self._report_diagnostics(runtime.resources.diagnostics)
            self._report_diagnostics(self.current_session.diagnostics)
            await self._refresh_context_inspection()
            self._update_status()
            self.query_one("#prompt", PromptInput).focus()
        except Exception as error:
            # 启动失败时仍保留可退出的 Textual 界面，而不是让异常穿透事件循环。
            self._show_error(f"Startup failed: {error}")
            self.query_one("#status-bar", Static).update("startup failed")
            self.query_one("#composer-hint", Static).update("Startup failed · Ctrl+Q quit")

    async def _build_runtime(self) -> HostRuntime:
        """调用注入的测试工厂，或构建真实的交互式 Host 运行时。"""

        if self._runtime_factory is not None:
            return await self._runtime_factory(self.cwd)
        return await build_interactive_runtime(
            self.cwd,
            approval_resolver=self._resolve_approval,
        )

    async def _resolve_approval(self, call: ToolCall, tool: Tool) -> AskResolution:
        """在 Textual worker 内等待人工审批，并对异常执行默认拒绝。"""

        try:
            result = await self.push_screen_wait(ApprovalPromptScreen(call, tool))
        except Exception:
            return AskResolution.DENY
        return result if isinstance(result, AskResolution) else AskResolution.DENY

    async def _render_current_conversation(self) -> None:
        """从当前 Session 分支重建 Transcript 的持久化展示。"""

        runtime, handle = self._require_session()
        transcript = self.query_one("#transcript", TranscriptView)
        await transcript.remove_children()
        # Tool Result Entry 与发起调用的 Assistant Entry 分开持久化，渲染时按 call_id 重连。
        tool_steps: dict[str, ModelStepView] = {}
        view = await materialize_presentation(runtime.storage, handle)
        for entry in view.entries:
            if isinstance(entry, UserMessageEntry):
                await transcript.mount(UserMessageView(entry.content))
            elif isinstance(entry, AssistantMessageEntry):
                step = ModelStepView(
                    content=entry.content,
                    reasoning=entry.reasoning,
                    tool_calls=entry.tool_calls,
                )
                await transcript.mount(step)
                tool_steps.update(dict.fromkeys(step.tool_call_ids, step))
            elif isinstance(entry, ToolResultEntry):
                step = tool_steps.get(entry.result.call_id)
                if step is not None:
                    step.complete_tool(entry.result)
            elif isinstance(entry, CompactionEntry):
                await transcript.mount(CompactionEntryView(entry.summary))

    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        """把提交内容路由为斜杠命令、排队草稿或一个新 Run。"""

        text = message.text
        if not text.strip() or self._runtime is None or self.current_session is None:
            return
        # 斜杠命令属于 Host 交互路由，不会作为普通消息发给 Model。
        if text.lstrip().startswith("/"):
            self.run_worker(self._execute_command(text), group="commands", exclusive=True)
            return
        # Session 变更会替换 handle；期间保留草稿，避免消息发到错误分支。
        if self._session_operation_active:
            self.query_one("#prompt", PromptInput).load_text(text)
            self._show_error("A Session operation is still running; the message remains editable")
            return
        # 一个 drain 周期串行等待每个 Run 完成，因此普通 follow-up 保持 FIFO。
        if self._draining:
            await self._enqueue(text)
            return
        self._draining = True
        self._update_composer_hint()
        self.run_worker(self._run_message(text), group="runs", exclusive=True)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """随 Prompt 内容调整高度，并显示或隐藏斜杠命令候选。"""

        if event.text_area.id != "prompt":
            return
        self.query_one("#prompt", PromptInput).resize_for_content()
        completion = self.query_one("#command-completion", Static)
        command_matches = self._slash_command_matches(event.text_area.text)
        if command_matches is None:
            completion.display = False
            self._update_composer_hint()
            return
        _, matches = command_matches
        completion.update("\n".join(matches) if matches else "No matching slash commands")
        completion.display = True
        self._update_composer_hint()

    def on_prompt_input_completion_requested(
        self,
        message: PromptInput.CompletionRequested,
    ) -> None:
        """在符合条件时补全命令，否则保留 Textual 的 Tab 焦点导航。"""

        prompt = message.prompt
        text = prompt.text
        eligible = prompt.selection.start == prompt.selection.end and prompt.cursor_at_end_of_text
        command_matches = self._slash_command_matches(text)
        if not eligible or command_matches is None:
            self.action_focus_next()
            return
        token, matches = command_matches
        if not matches:
            self.action_focus_next()
            return
        # 精确匹配优先；唯一匹配补全全名；多匹配只扩展共同前缀。
        if token in matches:
            replacement = f"{token} "
        elif len(matches) == 1:
            replacement = f"{matches[0]} "
        else:
            replacement = commonprefix(matches)
        # 使用 TextArea.replace 保留正常编辑的光标和 undo 语义。
        if replacement != token:
            token_start = len(text) - len(token)
            prompt.replace(
                replacement,
                (0, token_start),
                (0, len(text)),
                maintain_selection_offset=False,
            )
        prompt.focus()

    def _slash_command_matches(self, text: str) -> tuple[str, tuple[str, ...]] | None:
        """仅为单行、单 token 的斜杠草稿返回有序候选。"""

        token = text.lstrip()
        if (
            "\n" in text
            or not token.startswith("/")
            or any(character.isspace() for character in token)
        ):
            return None
        return token, tuple(name for name in self._command_names() if name.startswith(token))

    def _command_names(self) -> tuple[str, ...]:
        """合并内置、Skill 和 MCP Prompt 命令并去重。"""

        static = set(BUILTIN_COMMANDS)
        dynamic: list[str] = []
        if self._runtime is not None:
            for name in self._runtime.resources.skill_discovery.skills:
                command = f"/{name}"
                if command not in static:
                    dynamic.append(command)
            dynamic.extend(
                command
                for command in self._mcp_prompts
                if command not in static and command not in dynamic
            )
        return tuple((*BUILTIN_COMMANDS, *sorted(dynamic)))

    async def _enqueue(self, text: str) -> None:
        """把 Run 期间提交的输入保存在可见但未持久化的 Queue 中。"""

        pending = PendingMessage(uuid4().hex, text)
        self._pending.append(pending)
        row = QueueMessageRow(pending)
        await self.query_one("#queue", Vertical).mount(row)
        self._update_composer_hint()
        # mount 期间可能恰逢 Steer 被 Hook 消费；此时清理迟到挂载的行。
        if pending not in self._pending and row.is_mounted:
            await row.remove()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """路由 Queue 行的移除、Queue/Steer 切换和编辑操作。"""

        button_id = event.button.id
        if button_id is None or "-" not in button_id:
            return
        action, pending_id = button_id.split("-", 1)
        pending = next((item for item in self._pending if item.id == pending_id), None)
        if pending is None:
            return
        if action == "remove":
            await self._remove_pending(pending)
        elif action == "toggle":
            # Steer 必须绑定当前 Run 代次，不能意外注入下一次 Run。
            if pending.disposition is QueueDisposition.QUEUE:
                if not self._run_active or self._active_run_generation is None:
                    self._show_error("Steer requires an active Run")
                    return
                pending.disposition = QueueDisposition.STEER
                pending.target_run_generation = self._active_run_generation
            else:
                pending.disposition = QueueDisposition.QUEUE
                pending.target_run_generation = None
            self.query_one(f"#pending-{pending.id}", QueueMessageRow).refresh_pending(pending)
            self._update_composer_hint()
            if pending.disposition is QueueDisposition.QUEUE:
                # 若切回 Queue 时 Run 已结束，立即启动后续 FIFO drain。
                await self._start_pending_drain()
        elif action == "edit":
            # 编辑会把草稿移回 Prompt；它仍未成为 Session Entry。
            self.query_one("#prompt", PromptInput).load_text(pending.text)
            await self._remove_pending(pending)
            self.query_one("#prompt", PromptInput).focus()

    async def _remove_pending(self, pending: PendingMessage) -> None:
        """同时从内存 Queue 和已挂载行中移除一条草稿。"""

        if pending in self._pending:
            self._pending.remove(pending)
        rows = self.query(f"#pending-{pending.id}").nodes
        if rows:
            await rows[0].remove()
        self._update_composer_hint()

    async def _start_pending_drain(self) -> None:
        """在没有现行 drain 时取出首条普通 Queue 消息启动 Run。"""

        if self._draining or self._shutting_down:
            return
        text = await self._take_next_pending()
        if text is None:
            return
        self._draining = True
        self.run_worker(self._run_message(text), group="runs", exclusive=True)

    async def _execute_command(self, text: str) -> None:
        """解析并执行一个 Host 斜杠命令。"""

        try:
            parts = shlex.split(text)
        except ValueError as error:
            self._show_error(f"Invalid command: {error}")
            return
        if not parts:
            return
        command, *arguments = parts
        # 会替换 Session handle 或审批状态的命令必须与 Run 串行。
        session_operation = command in {
            "/new",
            "/resume",
            "/fork",
            "/tree",
            "/name",
            "/model",
            "/compact",
            "/permissions",
        }
        if session_operation and self._draining:
            self._show_error(f"{command} requires the current Run to finish or be cancelled")
            return
        if session_operation:
            self._session_operation_active = True
            self._update_composer_hint()
        try:
            runtime, handle = self._require_session()
            # Session 命令只调用共享服务；Host 负责保存服务返回的新不可变 handle。
            if command == "/new":
                if arguments:
                    raise ValueError("usage: /new")
                model_id, _ = resolve_available_model(
                    runtime.settings.default_model,
                    available_models=runtime.available_models,
                    missing_message="no Model was selected",
                )
                self.current_session = await create_session(runtime.storage, model=model_id)
            elif command == "/resume":
                if len(arguments) > 1:
                    raise ValueError("usage: /resume [session-id]")
                session_id = arguments[0] if arguments else await self._select_session(runtime)
                if session_id is None:
                    return
                self.current_session = await resume_session(runtime.storage, session_id)
            elif command == "/name":
                if not arguments:
                    raise ValueError("usage: /name <text>")
                self.current_session = await rename_session(
                    runtime.storage,
                    handle,
                    " ".join(arguments),
                )
            elif command == "/fork":
                if len(arguments) > 1:
                    raise ValueError("usage: /fork [entry-id]")
                entry_id = arguments[0] if arguments else await self._select_history_entry(handle)
                if entry_id is None:
                    return
                self.current_session = await fork_session(
                    runtime.storage,
                    handle,
                    entry_id,
                )
            elif command == "/tree":
                if len(arguments) > 1:
                    raise ValueError("usage: /tree [leaf-id]")
                leaf_id = arguments[0] if arguments else await self._select_leaf(handle)
                if leaf_id is None:
                    return
                self.current_session = await switch_leaf(runtime.storage, handle, leaf_id)
            elif command == "/session":
                # 只读命令展示当前 handle 元数据，不触碰 Session 持久化状态。
                if arguments:
                    raise ValueError("usage: /session")
                metadata = handle.metadata
                content = "\n".join(
                    (
                        f"Session ID: {metadata.id}",
                        f"Name: {metadata.name or '-'}",
                        f"Model: {metadata.model or '-'}",
                        f"Leaf: {metadata.leaf_id or '-'}",
                        f"Origin: {metadata.origin}",
                        f"Parent Session: {metadata.parent_session_id or '-'}",
                        f"Fork point: {metadata.fork_point_entry_id or '-'}",
                        f"Workspace: {self.cwd}",
                        f"Session file: {handle.session_file}",
                    )
                )
                await self.push_screen_wait(InfoScreen("Current Session", content))
                return
            elif command == "/context":
                # 检查器接收冻结快照，不持有可继续演化的 Session 服务对象。
                if arguments:
                    raise ValueError("usage: /context")
                await self._refresh_context_inspection()
                inspection = self._context_inspection
                assert inspection is not None
                self._report_diagnostics(inspection.diagnostics)
                await self.push_screen_wait(ContextInspectorScreen(inspection))
                return
            elif command == "/mcp":
                if arguments:
                    raise ValueError("usage: /mcp")
                counts = runtime.resources.mcp.server_tool_counts
                content = (
                    "\n".join(f"{server_id}: {count} Tools" for server_id, count in counts)
                    if counts
                    else "No connected MCP servers."
                )
                await self.push_screen_wait(InfoScreen("Connected MCP servers", content))
                return
            elif command == "/model":
                if len(arguments) > 1:
                    raise ValueError("usage: /model [model-id]")
                requested_model = arguments[0] if arguments else await self._select_model(runtime)
                if requested_model is None:
                    return
                model_id, _ = resolve_available_model(
                    requested_model,
                    available_models=runtime.available_models,
                    missing_message="select one available Model",
                )
                self.current_session = await select_model(runtime.storage, handle, model_id)
            elif command == "/compact":
                _model_id, model_info = self._selected_model(runtime, handle)
                self.current_session = await manual_compact(
                    handle,
                    storage=runtime.storage,
                    settings=runtime.settings,
                    model=runtime.model,
                    model_info=model_info,
                    tools=runtime.resources.tools,
                    stable_instructions=runtime.resources.stable_instructions,
                    focus=" ".join(arguments) or None,
                )
            elif command == "/permissions":
                if len(arguments) > 1:
                    raise ValueError("usage: /permissions [default|accept_edits|plan|bypass]")
                if runtime.approval_policy is None:
                    raise RuntimeError("interactive Approval Policy is unavailable")
                modes = {
                    mode.name: mode
                    for mode in (DEFAULT_MODE, ACCEPT_EDITS_MODE, PLAN_MODE, BYPASS_MODE)
                }
                mode_name = arguments[0] if arguments else await self._select_permission(modes)
                if mode_name is None:
                    return
                try:
                    runtime.approval_policy.mode = modes[mode_name]
                except KeyError:
                    raise ValueError(f"unknown Approval Policy mode: {mode_name}") from None
            elif command == "/quit":
                if arguments:
                    raise ValueError("usage: /quit")
                await self.action_quit()
                return
            elif command in self._mcp_prompts and command not in BUILTIN_COMMANDS:
                # MCP Prompt 只生成可编辑草稿；这里不会自动把它发送给 Model。
                prompt = self._mcp_prompts[command]
                values = self._parse_mcp_arguments(prompt, arguments)
                missing = any(
                    argument.required and not values.get(argument.name, "").strip()
                    for argument in prompt.arguments
                )
                if prompt.arguments and (not arguments or missing):
                    collected = await self.push_screen_wait(PromptArgumentsScreen(prompt, values))
                    if collected is None:
                        return
                    values = collected
                result = await runtime.resources.get_mcp_prompt(command, values)
                draft = self._mcp_prompt_draft(command, result)
                self.query_one("#prompt", PromptInput).load_text(draft)
                self.query_one("#prompt", PromptInput).focus()
                return
            elif (
                command.startswith("/")
                and command[1:] in runtime.resources.skill_discovery.skills
                and command not in BUILTIN_COMMANDS
            ):
                # Skill 同样展开为草稿，让用户在发起 Run 前能够检查和修改。
                if arguments:
                    raise ValueError(f"usage: {command}")
                draft = runtime.resources.invoke_skill(command[1:])
                self.query_one("#prompt", PromptInput).load_text(draft)
                self.query_one("#prompt", PromptInput).focus()
                return
            else:
                raise ValueError(f"unknown command: {command}")
            # 所有成功的 Session 变更都从持久化分支重建 Transcript 与 Context 状态。
            assert self.current_session is not None
            self._report_diagnostics(self.current_session.diagnostics)
            await self._render_current_conversation()
            await self._refresh_context_inspection()
            self._update_status()
        except Exception as error:
            self._show_error(str(error))
        finally:
            # 即便选择器取消或服务报错，也必须解除 Prompt 的 Session 操作锁。
            if session_operation:
                self._session_operation_active = False
                self._update_composer_hint()

    async def _select_session(self, runtime: HostRuntime) -> str | None:
        """按最近更新时间列出可恢复 Session，并返回所选 ID。"""

        handles = await list_session_handles(runtime.storage)
        handles.sort(
            key=lambda item: (item.metadata.updated_at, item.session_id),
            reverse=True,
        )
        options = tuple(
            (
                f"{item.metadata.name or item.session_id} · "
                f"{item.metadata.model or 'no Model'} · {item.session_id}",
                item.session_id,
            )
            for item in handles
        )
        return await self._choose("Resume Session", options, empty="No Sessions found")

    async def _select_history_entry(self, handle: SessionHandle) -> str | None:
        """从当前 Session 分支选择用户 Fork 的精确 Entry。"""

        runtime, _ = self._require_session()
        presentation = await materialize_presentation(runtime.storage, handle)
        options = tuple((self._entry_label(entry), entry.id) for entry in presentation.entries)
        return await self._choose("Fork after Entry", options, empty="Session has no Entries")

    async def _select_leaf(self, handle: SessionHandle) -> str | None:
        """选择当前 Session 树中的另一个叶节点。"""

        runtime, _ = self._require_session()
        leaves = await list_leaves(runtime.storage, handle)
        options = tuple(
            (f"{'Current · ' if leaf == handle.leaf_id else ''}{leaf}", leaf) for leaf in leaves
        )
        return await self._choose("Switch conversation leaf", options, empty="No leaves found")

    async def _select_model(self, runtime: HostRuntime) -> str | None:
        """展示运行时可用 Model 及其输入输出上限。"""

        options = tuple(
            (
                f"{model.id} · input {model.max_input_tokens or 'unknown'} · "
                f"output {model.max_output_tokens or 'unknown'}",
                model.id,
            )
            for model in runtime.available_models
        )
        return await self._choose("Select Model", options, empty="No Models available")

    async def _select_permission(self, modes: Mapping[str, object]) -> str | None:
        """选择交互式 Approval Policy 模式名称。"""

        options = tuple((name, name) for name in modes)
        return await self._choose(
            "Select Approval Policy",
            options,
            empty="No Approval Policy modes available",
        )

    async def _choose(
        self,
        title: str,
        options: tuple[tuple[str, str], ...],
        *,
        empty: str,
    ) -> str | None:
        """统一处理空选项错误和模态单选交互。"""

        if not options:
            raise ValueError(empty)
        return await self.push_screen_wait(SelectionScreen(title, options))

    @staticmethod
    def _entry_label(entry: object) -> str:
        """为 Fork 选择器生成长度受限且包含 Entry ID 的标签。"""

        if isinstance(entry, UserMessageEntry):
            detail = entry.content
        elif isinstance(entry, AssistantMessageEntry):
            detail = entry.content or entry.reasoning or f"{len(entry.tool_calls)} Tool Calls"
        elif isinstance(entry, ToolResultEntry):
            detail = entry.result.error or entry.result.output
        elif isinstance(entry, CompactionEntry):
            detail = entry.summary
        else:
            detail = type(entry).__name__
        preview = " ".join(detail.split())
        if len(preview) > 72:
            preview = f"{preview[:69]}..."
        entry_type = getattr(entry, "entry_type", type(entry).__name__)
        entry_id = getattr(entry, "id", "")
        return f"{entry_type} · {preview or '(empty)'} · {entry_id}"

    @staticmethod
    def _parse_mcp_arguments(prompt: McpPrompt, arguments: list[str]) -> dict[str, str]:
        """按 MCP Prompt 声明校验并解析 ``name=value`` 参数。"""

        advertised = {argument.name: argument for argument in prompt.arguments}
        values: dict[str, str] = {}
        for item in arguments:
            if "=" not in item:
                raise ValueError(f"MCP Prompt arguments use name=value syntax: {item!r}")
            name, value = item.split("=", 1)
            if name not in advertised:
                raise ValueError(f"unknown MCP Prompt argument: {name}")
            values[name] = value
        return values

    @staticmethod
    def _mcp_prompt_draft(command: str, result: McpPromptResult) -> str:
        """把仅含文本的 MCP Prompt 结果转换为用户可编辑草稿。"""

        parts = [f"MCP Prompt {command}"]
        if result.description:
            parts.append(result.description)
        for message in result.messages:
            if message.content.get("type") != "text" or not isinstance(
                message.content.get("text"), str
            ):
                raise ValueError(
                    f"unsupported MCP Prompt content for role {message.role!r}; "
                    "only text is editable"
                )
            parts.append(f"[{message.role}]\n{message.content['text']}")
        return "\n\n".join(parts)

    async def _run_message(self, text: str) -> None:
        """串行执行首条消息及其后所有普通 Queue 消息。"""

        next_text: str | None = text
        try:
            # 每条 Queue 消息启动独立 Run；必须等待前一 Run 完整清理后再发送下一条。
            while next_text is not None and not self._shutting_down:
                await self._run_one_message(next_text)
                next_text = None if self._shutting_down else await self._take_next_pending()
        finally:
            self._draining = False
            self._update_composer_hint()

    async def _run_one_message(self, text: str) -> None:
        """为一条用户输入启动、观察并收尾一个 bounded Run。"""

        runtime, handle = self._require_session()
        # 代次把 Steer 精确绑定到当前 Run，避免终态附近的竞态泄漏到下一次 Run。
        self._run_generation += 1
        self._active_run_generation = self._run_generation
        self._active_run_started = False
        self._run_active = True
        self._update_status()
        transcript = self.query_one("#transcript", TranscriptView)
        # 先乐观呈现用户输入；若服务在持久化前失败，异常路径会移除并恢复草稿。
        user_view = UserMessageView(text)
        await transcript.mount(user_view)
        if self._shutting_down:
            if user_view.is_mounted:
                await user_view.remove()
            self._active_run_started = False
            self._cancel_requested = False
            self._active_run_generation = None
            self._run_active = False
            return

        async def execute() -> None:
            """调用共享 Session 服务，并保存它返回的新 handle 与 Run 结果。"""

            assert self.current_session is not None
            _model_id, model_info = self._selected_model(runtime, handle)
            # Host 注入 Event consumer 和 Steer Hook，但 Run 循环仍完全属于 Harness。
            updated, result = await send_message(
                self.current_session,
                text,
                storage=runtime.storage,
                settings=runtime.settings,
                model=runtime.model,
                model_info=model_info,
                tools=runtime.resources.tools,
                dispatcher=runtime.resources.dispatcher,
                stable_instructions=runtime.resources.stable_instructions,
                max_steps=self._max_steps,
                hooks=Hooks(inject_messages=self._inject_messages),
                events=self,
                lifecycle=runtime.resources.agents,
            )
            self.current_session = updated
            if self._shutting_down:
                return
            self._report_diagnostics(updated.diagnostics)
            self._show_run_status(result.status, result.error)

        # 单独 Task 让 Escape 能通过 asyncio cancellation 取消正在等待的 Run。
        self._active_run_task = asyncio.create_task(execute())
        try:
            await self._active_run_task
        except Exception as error:
            # 不确定用户 Entry 是否已写入时，以 Session 存储为事实来源重新读取。
            persisted = False
            if user_view.is_mounted:
                await user_view.remove()
            if self.current_session is not None:
                try:
                    refreshed = await resume_session(
                        runtime.storage,
                        self.current_session.session_id,
                    )
                    presentation = await materialize_presentation(runtime.storage, refreshed)
                    persisted = any(
                        isinstance(entry, UserMessageEntry)
                        and entry.parent_id == handle.leaf_id
                        and entry.content == text
                        for entry in presentation.entries
                    )
                    self.current_session = refreshed
                    await self._render_current_conversation()
                except Exception:
                    pass
            # 仅在确认未持久化时恢复草稿，避免用户重复发送已经落盘的请求。
            if not persisted and not self._shutting_down:
                prompt = self.query_one("#prompt", PromptInput)
                prompt.load_text(text)
                prompt.focus()
            self._show_error(f"Run failed: {error}")
        finally:
            # 先清除 Run 标志，再刷新 Context；状态栏因此不会继续显示 updating。
            self._active_run_task = None
            self._active_run_started = False
            self._cancel_requested = False
            self._active_run_generation = None
            self._run_active = False
            if not self._shutting_down:
                try:
                    await self._refresh_context_inspection()
                except Exception as error:
                    self._context_inspection = None
                    self._show_error(f"Context status unavailable: {error}")
                self._update_status()

    async def _take_next_pending(self) -> str | None:
        """移除并返回 FIFO 顺序中的下一条普通 Queue 消息。"""

        if not self._pending:
            return None
        queued = next(
            (item for item in self._pending if item.disposition is QueueDisposition.QUEUE),
            None,
        )
        if queued is None:
            return None
        text = queued.text
        await self._remove_pending(queued)
        return text

    async def _inject_messages(self) -> list[str]:
        """在 Harness Step 边界取出只属于当前 Run 代次的 Steer 消息。"""

        steers = [
            pending
            for pending in self._pending
            if pending.disposition is QueueDisposition.STEER
            and pending.target_run_generation == self._active_run_generation
        ]
        messages = [pending.text for pending in steers]
        for pending in steers:
            await self._remove_pending(pending)
        return messages

    async def emit(self, event: RunEvent) -> None:
        """仅为界面展示消费共享 Run Event，不改变 Harness 行为。"""

        if isinstance(event, RunStarted):
            # RunStarted 建立了真正的 Harness Run；此前收到的 Escape 在此转为 Task 取消。
            self._active_run_started = True
            if self._cancel_requested:
                task = self._active_run_task
                if task is not None and not task.done():
                    task.cancel()
        if self._shutting_down:
            return
        transcript = self.query_one("#transcript", TranscriptView)
        # (run_id, step_index) 将交错 Event 路由到唯一的 Model Step 视图。
        if isinstance(event, ModelCallStarted):
            key = (event.run_id, event.step_index)
            step = ModelStepView()
            self._step_views[key] = step
            await transcript.mount(step)
        elif isinstance(event, ModelCallDelta):
            key = (event.run_id, event.step_index)
            self._step_views[key].apply_delta(event.delta)
        elif isinstance(event, ModelCallCompleted):
            key = (event.run_id, event.step_index)
            await self._step_views[key].complete_response(event.response)
        elif isinstance(event, ToolCallStarted):
            key = (event.run_id, event.step_index)
            await self._step_views[key].start_tool(event.call)
        elif isinstance(event, ToolCallCompleted):
            key = (event.run_id, event.step_index)
            self._step_views[key].complete_tool(event.result)
        elif isinstance(event, RunFinished):
            # Run 终态清理空的实时占位符；Run 结果边界由服务返回后统一渲染。
            for key in [key for key in self._step_views if key[0] == event.run_id]:
                step = self._step_views.pop(key)
                await step.finish()
                if step.is_empty and step.is_mounted:
                    await step.remove()
        transcript.scroll_end(animate=False)

    def action_cancel_run(self) -> None:
        """让 Escape 优先关闭 Context 检查器，否则请求取消当前 Run。"""

        if isinstance(self.screen, ContextInspectorScreen):
            self.screen.action_close()
            return
        self._request_run_cancellation()

    def _request_run_cancellation(self) -> None:
        """取消已启动 Run，或记住 RunStarted 之前到达的取消请求。"""

        task = self._active_run_task
        if task is not None and not task.done():
            if self._active_run_started:
                task.cancel()
            else:
                self._cancel_requested = True
        elif self._run_active or self._draining:
            self._cancel_requested = True

    async def action_quit(self) -> None:
        """先关闭运行时拥有的异步资源，再退出 Textual App。"""

        await self._close_runtime()
        self.exit()

    async def on_unmount(self) -> None:
        """在任何卸载路径上执行幂等运行时清理。"""

        await self._close_runtime()

    async def _close_runtime(self) -> None:
        """幂等取消活动 Run，并等待运行时资源全部关闭。"""

        if self._runtime_closed:
            return
        self._runtime_closed = True
        self._shutting_down = True
        # 必须先取消并等待 Run，使其拥有的子任务完成清理，再关闭共享传输资源。
        self._request_run_cancellation()
        task = self._active_run_task
        if task is not None and not task.done():
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self._runtime is not None:
            try:
                await self._runtime.close()
            except Exception:
                pass

    def _selected_model(
        self,
        runtime: HostRuntime,
        handle: SessionHandle,
    ) -> tuple[str, ModelInfo | None]:
        """按 Session 分支设置和运行时默认值解析当前 Model。"""

        return resolve_available_model(
            handle.metadata.model,
            runtime.settings.default_model,
            available_models=runtime.available_models,
            missing_message="no Model was selected",
        )

    def _require_session(self) -> tuple[HostRuntime, SessionHandle]:
        """返回已就绪运行时与 Session handle，否则给出明确启动错误。"""

        if self._runtime is None or self.current_session is None:
            raise RuntimeError("interactive runtime is not ready")
        return self._runtime, self.current_session

    def _update_status(self) -> None:
        """按终端宽度更新 Session、Model、Context 与审批状态栏。"""

        if self.current_session is None:
            return
        metadata = self.current_session.metadata
        identity = metadata.name or metadata.id[:8]
        model = metadata.model or "-"
        approval = (
            self._runtime.approval_policy.approval_mode_name
            if self._runtime is not None and self._runtime.approval_policy is not None
            else "unavailable"
        )
        width = self.size.width
        context = self._context_status(compact=width < 70, detailed=width >= 120)
        # 窄屏逐步省略方向信息，但始终保留 Model 和诚实的 Context 状态。
        if width < 55:
            text = f"Model {model} · {context}"
        elif width < 80:
            text = f"Model {model} · {context} · Approval {approval}"
        else:
            text = f"Session {identity} · Model {model} · {context} · Approval {approval}"
        status = self.query_one("#status-bar", Static)
        status.update(text)
        status.set_class(self._context_near_safe_limit(), "context-warning")
        self._update_composer_hint()

    def _context_status(self, *, compact: bool, detailed: bool) -> str:
        """格式化 Context 容量；未知上限时不推断利用率。"""

        label = "Ctx" if compact else "Context"
        if self._run_active:
            return f"{label} updating…"
        inspection = self._context_inspection
        if inspection is None:
            return f"{label} unavailable"
        estimate = inspection.estimate.tokens
        limit = inspection.effective_input_limit
        utilization = inspection.utilization_percent
        if limit is None or utilization is None:
            return f"{label} ~{estimate} · limit unknown"
        safe = inspection.safe_prompt_limit
        warning = ""
        # 安全提示基于 safe prompt limit，而利用率仍基于 effective input limit。
        if safe is not None and estimate >= safe:
            warning = " · over safe limit"
        elif safe is not None and estimate >= safe * 0.8:
            warning = " · near safe limit"
        if detailed:
            return (
                f"{label} ~{estimate}/{limit} ({utilization:.1f}%) · "
                f"safe {safe if safe is not None else 'unknown'}{warning}"
            )
        return f"{label} {utilization:.1f}%{warning}"

    def _context_near_safe_limit(self) -> bool:
        """判断状态栏是否应使用接近安全上限的警告样式。"""

        if self._run_active or self._context_inspection is None:
            return False
        safe = self._context_inspection.safe_prompt_limit
        return safe is not None and self._context_inspection.estimate.tokens >= safe * 0.8

    def _update_composer_hint(self) -> None:
        """根据补全、Session 操作、Run 与屏幕宽度显示当前可用操作。"""

        hints = self.query("#composer-hint")
        if not hints:
            return
        completion_nodes = self.query("#command-completion")
        completion_visible = bool(completion_nodes and completion_nodes.first(Static).display)
        # 分支顺序体现交互优先级：补全提示覆盖一般 Run/Ready 提示。
        if completion_visible:
            text = "Command · Tab complete · Enter run · Ctrl+P palette"
        elif self._session_operation_active:
            text = "Updating Session…"
        elif self._run_active or self._draining:
            pending = f" · {len(self._pending)} pending" if self._pending else ""
            state = "Running" if self._run_active else "Starting Run"
            follow_up = "Enter queues" if self.size.width < 70 else "Enter queues a follow-up"
            text = f"{state}{pending} · {follow_up} · Esc cancel"
        elif self.size.width < 72:
            text = "Ready · Enter send · Shift+Enter newline"
        else:
            text = "Ready · Enter send · Shift+Enter newline · / commands · Ctrl+P palette"
        hints.first(Static).update(text)

    async def _refresh_context_inspection(self) -> None:
        """从当前 handle 重建只读 Context 检查快照。"""

        runtime, handle = self._require_session()
        _model_id, model_info = self._selected_model(runtime, handle)
        self._context_inspection = await inspect_context(
            runtime.storage,
            handle,
            settings=runtime.settings,
            model_info=model_info,
            tools=runtime.resources.tools,
            instructions=runtime.resources.instruction_assembly,
        )

    def on_resize(self) -> None:
        """终端变化后刷新响应式状态栏和 Prompt 高度。"""

        self._update_status()
        prompt_nodes = self.query("#prompt")
        if prompt_nodes:
            self.call_after_refresh(prompt_nodes.first(PromptInput).resize_for_content)

    def _report_diagnostics(self, diagnostics: tuple[object, ...]) -> None:
        """去重、脱敏并把新的运行时或 Session 诊断追加到 Transcript。"""

        for diagnostic in diagnostics:
            safe = redact_text(str(diagnostic))
            if safe and safe not in self._reported_diagnostics:
                self._reported_diagnostics.add(safe)
                self.query_one("#transcript", TranscriptView).mount(
                    RunStatusView(f"Warning: {safe}")
                )

    def _show_run_status(self, status: RunStatus, error: Exception | None) -> None:
        """把 Run 终态渲染为低强调边界或显式错误卡片。"""

        transcript = self.query_one("#transcript", TranscriptView)
        if status is RunStatus.COMPLETED:
            transcript.mount(RunBoundaryView(self._clock()))
        elif status is RunStatus.FAILED:
            detail = redact_text(str(error)) if error is not None else "unknown failure"
            self._show_error(f"Run failed: {detail}")
        elif status is RunStatus.MAX_STEPS:
            transcript.mount(
                RunBoundaryView(
                    self._clock(),
                    status_label=f"Step limit ({self._max_steps})",
                )
            )
        elif status is RunStatus.CANCELLED:
            transcript.mount(RunBoundaryView(self._clock(), status_label="Cancelled"))

    def _show_error(self, message: str) -> None:
        """脱敏后在 Transcript 中显示操作失败。"""

        safe = redact_text(message) or "Operational failure"
        self.query_one("#transcript", TranscriptView).mount(RunStatusView(safe, failed=True))


def run(
    *,
    initial_session: SessionHandle | None = None,
    cwd: Path | None = None,
) -> None:
    """创建并启动 Phi 的 Textual Host。"""

    PhiApp(initial_session=initial_session, cwd=cwd).run()
