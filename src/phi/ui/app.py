from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Footer, Static, TextArea

from phi.bootstrap import HostRuntime, build_interactive_runtime
from phi.cli.model_selection import resolve_available_model
from phi.harness import (
    Hooks,
    ModelCallCompleted,
    ModelCallDelta,
    ModelCallStarted,
    RunEvent,
    RunFinished,
    RunResult,
    RunStarted,
    RunStatus,
    ToolCallCompleted,
    ToolCallStarted,
)
from phi.mcp import McpPrompt, McpPromptResult
from phi.model import ContentDelta, ModelInfo, ReasoningDelta, ToolCall, ToolCallDelta
from phi.sessions import (
    AssistantMessageEntry,
    CompactionEntry,
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
    AssistantMessageView,
    CompactionEntryView,
    PendingMessage,
    PromptInput,
    QueueDisposition,
    QueueMessageRow,
    ReasoningView,
    RunStatusView,
    ToolCallView,
    TranscriptView,
    UserMessageView,
)

type RuntimeFactory = Callable[[Path], Awaitable[HostRuntime]]

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
    """Interactive Host over one cwd-scoped runtime and immutable Session handle."""

    CSS = """
    Screen { layout: vertical; }
    #transcript { height: 1fr; padding: 0 1; }
    .user-message { margin: 1 0; padding: 1 2; background: $boost; }
    .assistant-message { margin: 1 0; padding: 0 2; }
    .reasoning-message { margin: 0 2; padding: 0 1; color: $text-muted; height: auto; }
    .tool-call { margin: 1 2; padding: 1; border: round $accent; height: auto; }
    .tool-call-content { height: auto; }
    .tool-call-progress { height: 1; }
    .tool-call.failed, .run-status.failed { border: round $error; color: $error; }
    .compaction-entry { margin: 1 2; padding: 1; border: dashed $secondary; height: auto; }
    #queue { height: auto; max-height: 10; }
    #command-completion {
        height: auto;
        max-height: 10;
        padding: 0 2;
        background: $panel;
        display: none;
    }
    .queue-row { height: auto; min-height: 3; padding: 0 1; }
    .queued-message { width: 1fr; height: auto; }
    .queue-row Button { width: auto; min-width: 8; margin: 0 0 0 1; }
    #prompt { height: 5; border: round $accent; }
    #status-bar { height: 1; padding: 0 1; background: $panel; }
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
    ) -> None:
        super().__init__()
        self.current_session = initial_session
        self.cwd = (cwd or Path.cwd()).expanduser().resolve()
        self._runtime_factory = runtime_factory
        self._max_steps = max_steps
        self._runtime: HostRuntime | None = None
        self._runtime_closed = False
        self._shutting_down = False
        self._active_run_task: asyncio.Task[None] | None = None
        self._active_run_started = False
        self._cancel_requested = False
        self._run_generation = 0
        self._active_run_generation: int | None = None
        self._run_active = False
        self._last_run_status: RunStatus | None = None
        self._draining = False
        self._session_operation_active = False
        self._pending: list[PendingMessage] = []
        self._reported_diagnostics: set[str] = set()
        self._assistant_views: dict[tuple[str, int], AssistantMessageView] = {}
        self._reasoning_views: dict[tuple[str, int], ReasoningView] = {}
        self._tool_views: dict[str, ToolCallView] = {}
        self._mcp_prompts: dict[str, McpPrompt] = {}
        self._provider_usage: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield TranscriptView(id="transcript")
        yield Vertical(id="queue")
        yield Static("", id="command-completion")
        yield PromptInput(id="prompt", placeholder="Ask Phi… (Shift+Enter for newline)")
        yield Static("Starting…", id="status-bar", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        try:
            runtime = await self._build_runtime()
            self._runtime = runtime
            if runtime.approval_policy is not None:
                runtime.approval_policy.set_resolver(self._resolve_approval)
            self._mcp_prompts = {
                prompt.command: prompt for prompt in await runtime.resources.list_mcp_prompts()
            }
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
            await self._render_current_conversation()
            self._report_diagnostics(runtime.resources.diagnostics)
            self._report_diagnostics(self.current_session.diagnostics)
            self._update_status()
            self.query_one("#prompt", PromptInput).focus()
        except Exception as error:
            self._show_error(f"Startup failed: {error}")
            self.query_one("#status-bar", Static).update("startup failed")

    async def _build_runtime(self) -> HostRuntime:
        if self._runtime_factory is not None:
            return await self._runtime_factory(self.cwd)
        return await build_interactive_runtime(
            self.cwd,
            approval_resolver=self._resolve_approval,
        )

    async def _resolve_approval(self, call: ToolCall, tool: Tool) -> AskResolution:
        try:
            result = await self.push_screen_wait(ApprovalPromptScreen(call, tool))
        except Exception:
            return AskResolution.DENY
        return result if isinstance(result, AskResolution) else AskResolution.DENY

    async def _render_current_conversation(self) -> None:
        runtime, handle = self._require_session()
        transcript = self.query_one("#transcript", TranscriptView)
        await transcript.remove_children()
        self._tool_views.clear()
        view = await materialize_presentation(runtime.storage, handle)
        for entry in view.entries:
            if isinstance(entry, UserMessageEntry):
                await transcript.mount(UserMessageView(entry.content))
            elif isinstance(entry, AssistantMessageEntry):
                await self._mount_assistant_entry(entry)
            elif isinstance(entry, ToolResultEntry):
                tool_view = self._tool_views.get(entry.result.call_id)
                if tool_view is not None:
                    tool_view.complete(entry.result.output, entry.result.error)
            elif isinstance(entry, CompactionEntry):
                await transcript.mount(CompactionEntryView(entry.summary))

    async def _mount_assistant_entry(self, entry: AssistantMessageEntry) -> None:
        transcript = self.query_one("#transcript", TranscriptView)
        if entry.content is not None:
            await transcript.mount(AssistantMessageView(entry.content))
        if entry.reasoning is not None:
            await transcript.mount(ReasoningView(entry.reasoning))
        for call in entry.tool_calls:
            view = ToolCallView(call.id, call.name, call.arguments)
            self._tool_views[call.id] = view
            await transcript.mount(view)

    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        text = message.text
        if not text.strip() or self._runtime is None or self.current_session is None:
            return
        if text.lstrip().startswith("/"):
            self.run_worker(self._execute_command(text), group="commands", exclusive=True)
            return
        if self._session_operation_active:
            self.query_one("#prompt", PromptInput).load_text(text)
            self._show_error("A Session operation is still running; the message remains editable")
            return
        if self._draining:
            await self._enqueue(text)
            return
        self._draining = True
        self.run_worker(self._run_message(text), group="runs", exclusive=True)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "prompt":
            return
        completion = self.query_one("#command-completion", Static)
        text = event.text_area.text.lstrip()
        if not text.startswith("/") or " " in text or "\n" in text:
            completion.display = False
            return
        matches = [name for name in self._command_names() if name.startswith(text)]
        completion.update("\n".join(matches) if matches else "No matching slash commands")
        completion.display = True

    def _command_names(self) -> tuple[str, ...]:
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
        pending = PendingMessage(uuid4().hex, text)
        self._pending.append(pending)
        row = QueueMessageRow(pending)
        await self.query_one("#queue", Vertical).mount(row)
        if pending not in self._pending and row.is_mounted:
            await row.remove()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
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
            if pending.disposition is QueueDisposition.QUEUE:
                await self._start_pending_drain()
        elif action == "edit":
            self.query_one("#prompt", PromptInput).load_text(pending.text)
            await self._remove_pending(pending)
            self.query_one("#prompt", PromptInput).focus()

    async def _remove_pending(self, pending: PendingMessage) -> None:
        if pending in self._pending:
            self._pending.remove(pending)
        rows = self.query(f"#pending-{pending.id}").nodes
        if rows:
            await rows[0].remove()

    async def _start_pending_drain(self) -> None:
        if self._draining or self._shutting_down:
            return
        text = await self._take_next_pending()
        if text is None:
            return
        self._draining = True
        self.run_worker(self._run_message(text), group="runs", exclusive=True)

    async def _execute_command(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as error:
            self._show_error(f"Invalid command: {error}")
            return
        if not parts:
            return
        command, *arguments = parts
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
        try:
            runtime, handle = self._require_session()
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
                        f"Session file: {handle.session_file}",
                    )
                )
                await self.push_screen_wait(InfoScreen("Current Session", content))
                return
            elif command == "/context":
                if arguments:
                    raise ValueError("usage: /context")
                _, model_info = self._selected_model(runtime, handle)
                inspection = await inspect_context(
                    runtime.storage,
                    handle,
                    settings=runtime.settings,
                    model_info=model_info,
                    tools=runtime.resources.tools,
                    stable_instructions=runtime.resources.stable_instructions,
                )
                self._report_diagnostics(inspection.diagnostics)
                await self.push_screen_wait(
                    ContextInspectorScreen(
                        inspection,
                        handle,
                        provider_usage=self._provider_usage or None,
                    )
                )
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
                if arguments:
                    raise ValueError(f"usage: {command}")
                draft = runtime.resources.invoke_skill(command[1:])
                self.query_one("#prompt", PromptInput).load_text(draft)
                self.query_one("#prompt", PromptInput).focus()
                return
            else:
                raise ValueError(f"unknown command: {command}")
            assert self.current_session is not None
            self._report_diagnostics(self.current_session.diagnostics)
            await self._render_current_conversation()
            self._update_status()
        except Exception as error:
            self._show_error(str(error))
        finally:
            if session_operation:
                self._session_operation_active = False

    async def _select_session(self, runtime: HostRuntime) -> str | None:
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
        runtime, _ = self._require_session()
        presentation = await materialize_presentation(runtime.storage, handle)
        options = tuple((self._entry_label(entry), entry.id) for entry in presentation.entries)
        return await self._choose("Fork after Entry", options, empty="Session has no Entries")

    async def _select_leaf(self, handle: SessionHandle) -> str | None:
        runtime, _ = self._require_session()
        leaves = await list_leaves(runtime.storage, handle)
        options = tuple(
            (f"{'Current · ' if leaf == handle.leaf_id else ''}{leaf}", leaf) for leaf in leaves
        )
        return await self._choose("Switch conversation leaf", options, empty="No leaves found")

    async def _select_model(self, runtime: HostRuntime) -> str | None:
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
        if not options:
            raise ValueError(empty)
        return await self.push_screen_wait(SelectionScreen(title, options))

    @staticmethod
    def _entry_label(entry: object) -> str:
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
        next_text: str | None = text
        try:
            while next_text is not None and not self._shutting_down:
                await self._run_one_message(next_text)
                next_text = None if self._shutting_down else await self._take_next_pending()
        finally:
            self._draining = False

    async def _run_one_message(self, text: str) -> None:
        runtime, handle = self._require_session()
        self._run_generation += 1
        self._active_run_generation = self._run_generation
        self._active_run_started = False
        self._run_active = True
        self._update_status()
        transcript = self.query_one("#transcript", TranscriptView)
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
            assert self.current_session is not None
            _model_id, model_info = self._selected_model(runtime, handle)
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
            self._last_run_status = result.status
            self._record_provider_usage(result)
            self._report_diagnostics(updated.diagnostics)
            self._show_run_status(result.status, result.error)

        self._active_run_task = asyncio.create_task(execute())
        try:
            await self._active_run_task
        except Exception as error:
            self._last_run_status = RunStatus.FAILED
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
            if not persisted and not self._shutting_down:
                prompt = self.query_one("#prompt", PromptInput)
                prompt.load_text(text)
                prompt.focus()
            self._show_error(f"Run failed: {error}")
        finally:
            self._active_run_task = None
            self._active_run_started = False
            self._cancel_requested = False
            self._active_run_generation = None
            self._run_active = False
            if not self._shutting_down:
                self._update_status()

    async def _take_next_pending(self) -> str | None:
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
        """Consume shared Run Events for presentation only."""

        if isinstance(event, RunStarted):
            self._active_run_started = True
            if self._cancel_requested:
                task = self._active_run_task
                if task is not None and not task.done():
                    task.cancel()
        if self._shutting_down:
            return
        transcript = self.query_one("#transcript", TranscriptView)
        if isinstance(event, ModelCallStarted):
            key = (event.run_id, event.step_index)
            assistant = AssistantMessageView()
            reasoning = ReasoningView()
            self._assistant_views[key] = assistant
            self._reasoning_views[key] = reasoning
            await transcript.mount(assistant)
        elif isinstance(event, ModelCallDelta):
            key = (event.run_id, event.step_index)
            if isinstance(event.delta, ContentDelta):
                self._assistant_views[key].append_content(event.delta.text)
            elif isinstance(event.delta, ReasoningDelta):
                reasoning = self._reasoning_views[key]
                if not reasoning.is_mounted:
                    await transcript.mount(reasoning)
                reasoning.append_content(event.delta.text)
            elif isinstance(event.delta, ToolCallDelta):
                return
        elif isinstance(event, ModelCallCompleted):
            key = (event.run_id, event.step_index)
            response = event.response
            if response.content is not None:
                self._assistant_views[key].set_content(response.content)
            if response.reasoning is not None:
                reasoning = self._reasoning_views[key]
                if not reasoning.is_mounted:
                    await transcript.mount(reasoning)
                reasoning.set_content(response.reasoning)
            if response.content is None and self._assistant_views[key].is_empty:
                await self._assistant_views[key].remove()
        elif isinstance(event, ToolCallStarted):
            view = ToolCallView(event.call.id, event.call.name, event.call.arguments)
            self._tool_views[event.call.id] = view
            await transcript.mount(view)
        elif isinstance(event, ToolCallCompleted):
            view = self._tool_views.get(event.call.id)
            if view is not None:
                view.complete(event.result.output, event.result.error)
        elif isinstance(event, RunFinished):
            for key in [key for key in self._assistant_views if key[0] == event.run_id]:
                assistant = self._assistant_views.pop(key)
                reasoning = self._reasoning_views.pop(key)
                if assistant.is_empty and assistant.is_mounted:
                    await assistant.remove()
                if reasoning.is_empty and reasoning.is_mounted:
                    await reasoning.remove()
        transcript.scroll_end(animate=False)

    def _record_provider_usage(self, result: RunResult) -> None:
        for step in result.steps:
            usage = step.response.usage
            if usage is None:
                continue
            values = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cached_tokens": usage.cached_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            }
            for name, value in values.items():
                if value is not None:
                    self._provider_usage[name] = self._provider_usage.get(name, 0) + value

    def action_cancel_run(self) -> None:
        self._request_run_cancellation()

    def _request_run_cancellation(self) -> None:
        task = self._active_run_task
        if task is not None and not task.done():
            if self._active_run_started:
                task.cancel()
            else:
                self._cancel_requested = True
        elif self._run_active or self._draining:
            self._cancel_requested = True

    async def action_quit(self) -> None:
        await self._close_runtime()
        self.exit()

    async def on_unmount(self) -> None:
        await self._close_runtime()

    async def _close_runtime(self) -> None:
        if self._runtime_closed:
            return
        self._runtime_closed = True
        self._shutting_down = True
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
        return resolve_available_model(
            handle.metadata.model,
            runtime.settings.default_model,
            available_models=runtime.available_models,
            missing_message="no Model was selected",
        )

    def _require_session(self) -> tuple[HostRuntime, SessionHandle]:
        if self._runtime is None or self.current_session is None:
            raise RuntimeError("interactive runtime is not ready")
        return self._runtime, self.current_session

    def _update_status(self) -> None:
        if self.current_session is None:
            return
        metadata = self.current_session.metadata
        identity = metadata.name or metadata.id
        state = "running" if self._run_active else "idle"
        if not self._run_active and self._last_run_status is not None:
            state = f"{state} · Last Run {self._last_run_status.value}"
        approval = (
            self._runtime.approval_policy.approval_mode_name
            if self._runtime is not None and self._runtime.approval_policy is not None
            else "unavailable"
        )
        self.query_one("#status-bar", Static).update(
            f"{state} · Model {metadata.model or '-'} · Session {identity} · "
            f"Approval {approval} · {self.cwd}"
        )

    def _report_diagnostics(self, diagnostics: tuple[object, ...]) -> None:
        for diagnostic in diagnostics:
            safe = redact_text(str(diagnostic))
            if safe and safe not in self._reported_diagnostics:
                self._reported_diagnostics.add(safe)
                self.query_one("#transcript", TranscriptView).mount(
                    RunStatusView(f"Warning: {safe}")
                )

    def _show_run_status(self, status: RunStatus, error: Exception | None) -> None:
        if status is RunStatus.COMPLETED:
            self.query_one("#transcript", TranscriptView).mount(RunStatusView("Run completed"))
        elif status is RunStatus.FAILED:
            detail = redact_text(str(error)) if error is not None else "unknown failure"
            self._show_error(f"Run failed: {detail}")
        elif status is RunStatus.MAX_STEPS:
            self.query_one("#transcript", TranscriptView).mount(
                RunStatusView(f"Run exhausted its Step budget ({self._max_steps})")
            )
        elif status is RunStatus.CANCELLED:
            self.query_one("#transcript", TranscriptView).mount(RunStatusView("Run cancelled"))

    def _show_error(self, message: str) -> None:
        safe = redact_text(message) or "Operational failure"
        self.query_one("#transcript", TranscriptView).mount(RunStatusView(safe, failed=True))


def run(
    *,
    initial_session: SessionHandle | None = None,
    cwd: Path | None = None,
) -> None:
    PhiApp(initial_session=initial_session, cwd=cwd).run()
