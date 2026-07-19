from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr
from textual.widgets import (
    Collapsible,
    LoadingIndicator,
    Markdown,
    Static,
    TabbedContent,
    TextArea,
    Tree,
)

from phi.bootstrap import HostRuntime, build_runtime_resources
from phi.instructions import PHI_BASE_INSTRUCTIONS
from phi.model import (
    ContentDelta,
    FinishEvent,
    Model,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ReasoningDelta,
    ScriptedModel,
    ToolCall,
    ToolCallDelta,
    Usage,
)
from phi.sessions import SessionStorage, materialize_conversation, resume_session
from phi.sessions.entries import Entry
from phi.sessions.metadata import SessionMetadata
from phi.sessions.storage import LoadedSession
from phi.settings import Settings
from phi.tools import (
    BYPASS_MODE,
    DEFAULT_MODE,
    ApprovalMode,
    RuleBasedApprovalPolicy,
    Tool,
    tool,
)
from phi.ui.app import PhiApp


@dataclass
class TuiRuntimeFactory:
    root: Path
    settings: Settings
    model: Model
    available_models: tuple[ModelInfo, ...] = (ModelInfo("model-a", 100_000),)
    approval_mode: ApprovalMode = BYPASS_MODE
    close_count: int = 0
    storage: SessionStorage | None = None
    extra_tools: tuple[Tool, ...] = ()

    async def __call__(self, cwd: Path) -> HostRuntime:
        policy = RuleBasedApprovalPolicy(self.approval_mode)
        resources = await build_runtime_resources(
            cwd,
            base_instructions=PHI_BASE_INSTRUCTIONS,
            approval_policy=policy,
            global_skill_root=self.root / "global-skills",
            global_agent_root=self.root / "global-agents",
            global_mcp_config_path=self.root / "global-mcp.json",
        )
        resources.tools.register_many(self.extra_tools)

        async def observe_close() -> None:
            self.close_count += 1

        return HostRuntime(
            settings=self.settings,
            model=self.model,
            available_models=self.available_models,
            storage=self.storage or SessionStorage(self.settings.session_dir),
            resources=resources,
            close_callback=observe_close,
            approval_policy=policy,
        )


def _settings(root: Path) -> Settings:
    return Settings(
        api_key=SecretStr("test-key"),
        base_url="https://proxy.example/v1",
        default_model="model-a",
        session_dir=root / "sessions",
    )


class GatedModel(ScriptedModel):
    def __init__(self, responses: list[ModelResponse | Exception]) -> None:
        super().__init__(responses)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._request_count = 0

    async def request_stream(self, request: ModelRequest):
        self._request_count += 1
        if self._request_count == 1:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        async for event in super().request_stream(request):
            yield event


class GatedLoadStorage(SessionStorage):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    async def load_state(self, session_id: str) -> LoadedSession:
        if self._armed:
            self._armed = False
            self.started.set()
            await self.release.wait()
        return await super().load_state(session_id)


class FailingAppendStorage(SessionStorage):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_next_append = True

    async def append_entries(
        self,
        session_id: str,
        *,
        expected_revision: int,
        entries: tuple[Entry, ...],
        metadata: SessionMetadata,
    ) -> LoadedSession:
        if self.fail_next_append:
            self.fail_next_append = False
            raise RuntimeError("simulated append failure")
        return await super().append_entries(
            session_id,
            expected_revision=expected_revision,
            entries=entries,
            metadata=metadata,
        )


class FragmentedToolModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__([])
        self.fragment_seen = asyncio.Event()
        self.release = asyncio.Event()

    async def request_stream(self, request: ModelRequest):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ReasoningDelta("checking the file")
            yield ToolCallDelta(
                index=0,
                id="read-fragmented",
                name="read",
                arguments_fragment='{"path":',
            )
            self.fragment_seen.set()
            await self.release.wait()
            yield ToolCallDelta(index=0, arguments_fragment='"input.txt"}')
            yield FinishEvent("tool_calls", {})
            return
        yield ContentDelta("done")
        yield FinishEvent("stop", {})


async def test_app_creates_session_and_sends_prompt_through_shared_service(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="Hello **from Phi**")])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        session_id = app.current_session.session_id
        assert "model-a" in str(app.query_one("#status-bar").render())
        await pilot.click("#prompt")
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await pilot.pause()
        await app.workers.wait_for_complete()

        transcript = app.query_one("#transcript")
        assert "You" in str(transcript.query_one(".user-message").render())
        assert "hello" in str(transcript.query_one(".user-message").render())
        assert "Hello" in transcript.query_one(".assistant-message", Markdown).source
        assert "Last Run completed" in str(app.query_one("#status-bar").render())

        storage = SessionStorage(factory.settings.session_dir)
        view = await materialize_conversation(
            storage,
            await resume_session(storage, session_id),
        )
        assert [entry.entry_type for entry in view.entries] == [
            "user_message",
            "assistant_message",
        ]

    assert factory.close_count == 1


async def test_context_status_tracks_known_capacity_and_active_run(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = GatedModel([ModelResponse(content="capacity changed")])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        initial = str(app.query_one("#status-bar").render())
        assert "Context ~" in initial
        assert "/100000" in initial
        assert "%" in initial
        assert "safe 83616" in initial

        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("grow the Context")
        await pilot.press("enter")
        await model.started.wait()
        assert "Context updating" in str(app.query_one("#status-bar").render())

        model.release.set()
        await app.workers.wait_for_complete()
        refreshed = str(app.query_one("#status-bar").render())
        assert "Context updating" not in refreshed
        assert "Last Run completed" in refreshed
        assert refreshed != initial
        grown_match = re.search(r"Context ~(\d+)", refreshed)
        assert grown_match is not None
        grown_tokens = int(grown_match.group(1))

        prompt.load_text("/new")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        reset = str(app.query_one("#status-bar").render())
        reset_match = re.search(r"Context ~(\d+)", reset)
        assert reset_match is not None
        assert int(reset_match.group(1)) < grown_tokens


async def test_context_status_and_explorer_are_honest_when_capacity_is_unknown(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        ScriptedModel([]),
        available_models=(ModelInfo("model-a"),),
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test(size=(56, 24)) as pilot:
        await pilot.pause()
        status = str(app.query_one("#status-bar").render())
        assert "Ctx ~" in status
        assert "limit unknown" in status
        assert "%" not in status

        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("/context")
        await pilot.press("enter")
        await pilot.pause()
        overview = str(app.screen.query_one("#context-overview-content").render())
        assert "Effective input limit: unknown" in overview
        assert "Safe prompt limit: unknown" in overview
        assert "Utilization: unavailable" in overview
        assert "%" not in overview

        await pilot.press("2")
        await pilot.pause()
        tree = app.screen.query_one("#context-contents-tree", Tree)
        detail = app.screen.query_one("#context-detail-scroll")
        assert tree.region.y < detail.region.y
        assert tree.region.width == detail.region.width
        assert "You are Phi" in str(app.screen.query_one("#context-content-detail").render())
        await pilot.press("escape")


async def test_session_commands_replace_the_current_immutable_handle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        original_id = app.current_session.session_id
        prompt = app.query_one("#prompt", TextArea)

        prompt.load_text("/name Project Alpha")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        assert app.current_session.metadata.name == "Project Alpha"
        assert app.current_session.revision == 1

        prompt.load_text("/new")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        assert app.current_session.session_id != original_id

        prompt.load_text(f"/resume {original_id}")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        assert app.current_session.session_id == original_id
        assert app.current_session.metadata.name == "Project Alpha"


async def test_follow_up_messages_are_visible_and_run_in_fifo_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = GatedModel(
        [
            ModelResponse(content="first answer"),
            ModelResponse(content="second answer"),
        ]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("first")
        await pilot.press("enter")
        await model.started.wait()

        prompt.load_text("second")
        await pilot.press("enter")
        await pilot.pause()
        assert "second" in str(app.query_one(".queued-message").render())

        model.release.set()
        await app.workers.wait_for_complete()
        assert [request.messages[-1] for request in model.requests] == [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        assert list(app.query(".queued-message")) == []

        assert app.current_session is not None
        storage = SessionStorage(factory.settings.session_dir)
        view = await materialize_conversation(storage, app.current_session)
        assert [entry.content for entry in view.entries if entry.entry_type == "user_message"] == [
            "first",
            "second",
        ]


async def test_context_command_opens_educational_request_explorer_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("call-1", "missing-tool", {"value": 1})],
            ),
            ModelResponse(
                content="answer",
                usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            ),
        ]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("question")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert len(model.requests) == 2
        assert app.current_session is not None
        revision = app.current_session.revision
        leaf_id = app.current_session.leaf_id

        prompt.load_text("/context")
        await pilot.press("enter")
        await pilot.pause()
        views = app.screen.query_one("#context-views", TabbedContent)
        assert views.active == "context-overview"
        overview = str(app.screen.query_one("#context-overview-content").render())
        assert "Model: model-a" in overview
        assert "Session path: 4 Entries" in overview
        assert "Conversation View: 4 Entries" in overview
        assert "Context: 4 selected messages" in overview
        assert "Model request: 5 messages" in overview
        assert "Final Token Estimate: ~" in overview
        assert "Latest applicable prompt Usage anchor: 10 tokens" in overview
        assert "Effective input limit: 100000 tokens" in overview
        assert "Safe prompt limit: 83616 tokens" in overview
        assert "Aggregate provider Usage" not in overview

        await pilot.press("3")
        await pilot.pause()
        assert views.active == "context-raw"
        raw = str(app.screen.query_one("#context-raw-request").render())
        assert '"model": "model-a"' in raw
        assert '"role": "system"' in raw
        assert '"content": "question"' in raw
        assert '"name": "missing-tool"' in raw
        assert "unknown_tool: missing-tool" in raw

        await pilot.press("2")
        await pilot.pause()
        assert views.active == "context-contents"
        detail = str(app.screen.query_one("#context-content-detail").render())
        assert "Phi base" in detail
        assert "Stable · included" in detail
        assert "You are Phi" in detail

        await pilot.press("down", "down")
        detail = str(app.screen.query_one("#context-content-detail").render())
        assert "Tool Registry" in detail
        assert "Registered · included" in detail
        assert '"name": "bash"' in detail

        await pilot.press("shift+left", "shift+down", "down", "down")
        detail = str(app.screen.query_one("#context-content-detail").render())
        assert "Assistant Tool Calls" in detail
        assert "Readable content" in detail
        assert "Tool Call: missing-tool" in detail
        assert '"arguments": "{\\"value\\":1}"' in detail
        await pilot.press("down")
        detail = str(app.screen.query_one("#context-content-detail").render())
        assert "Tool Result" in detail
        assert "unknown_tool: missing-tool" in detail
        await pilot.press("down")
        detail = str(app.screen.query_one("#context-content-detail").render())
        assert "Assistant message" in detail
        assert "answer" in detail

        await pilot.press("1")
        await pilot.pause()
        assert views.active == "context-overview"
        await pilot.press("2", "3")
        await pilot.pause()
        assert views.active == "context-raw"
        assert len(model.requests) == 2
        assert app.current_session.revision == revision
        assert app.current_session.leaf_id == leaf_id

        await pilot.press("escape")
        await app.workers.wait_for_complete()


async def test_model_command_updates_empty_branch_and_forks_after_model_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="answer from b")])
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a", 100_000), ModelInfo("model-b", 100_000)),
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        original_id = app.current_session.session_id
        prompt = app.query_one("#prompt", TextArea)

        prompt.load_text("/model model-b")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        assert app.current_session.session_id == original_id
        assert app.current_session.metadata.model == "model-b"

        prompt.load_text("question")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        source_id = app.current_session.session_id

        prompt.load_text("/model model-a")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        assert app.current_session.session_id != source_id
        assert app.current_session.metadata.parent_session_id == source_id
        assert app.current_session.metadata.model == "model-a"


async def test_interactive_approval_modal_controls_the_actual_tool_result(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-1",
                        "write",
                        {"path": "approved.txt", "content": "approved"},
                    )
                ]
            ),
            ModelResponse(content="done"),
        ]
    )
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        approval_mode=DEFAULT_MODE,
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("write the file")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        approval = app.screen
        assert "write" in str(approval.query_one("#approval-tool").render())
        assert "mutates_workspace" in str(approval.query_one("#approval-class").render())
        assert "approved.txt" in str(approval.query_one("#approval-arguments").render())
        assert app.query_one(".tool-call-progress", LoadingIndicator).display is True
        await pilot.click("#approval-once")
        await app.workers.wait_for_complete()

        assert (workspace / "approved.txt").read_text() == "approved"
        tool_card = app.query_one(".tool-call")
        tool_content = str(tool_card.query_one(".tool-call-content").render())
        assert "complete" in tool_content
        assert "approved.txt" in tool_content


async def test_slash_completion_invokes_model_disabled_skill_as_editable_draft(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill_root = workspace / ".phi" / "skills" / "draft"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: draft
description: Prepare a careful draft.
disable-model-invocation: true
---

Use this trusted draft instruction.
"""
    )
    model = ScriptedModel([])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        revision = app.current_session.revision
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("/")
        await pilot.pause()
        assert "/draft" in str(app.query_one("#command-completion").render())

        prompt.load_text("/draft")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert "Use this trusted draft instruction." in prompt.text
        assert app.current_session is not None
        assert app.current_session.revision == revision


async def test_namespaced_mcp_prompt_is_retrieved_as_editable_draft(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_root = workspace / ".phi"
    config_root.mkdir(parents=True)
    fixture = Path(__file__).parent / "mcp" / "stdio_fixture.py"
    (config_root / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "prompts": {
                        "command": sys.executable,
                        "args": [str(fixture)],
                    }
                }
            }
        )
    )
    model = ScriptedModel([])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("/mcp__")
        await pilot.pause()
        assert "/mcp__prompts__welcome" in str(app.query_one("#command-completion").render())

        prompt.load_text("/mcp")
        await pilot.press("enter")
        await pilot.pause()
        assert "prompts:" in str(app.screen.query_one("#info-content").render())
        assert "Tools" in str(app.screen.query_one("#info-content").render())
        await pilot.press("escape")
        await app.workers.wait_for_complete()

        prompt.load_text("/mcp__prompts__welcome")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#prompt-argument-0")
        await pilot.press("A", "d", "a")
        await pilot.click("#prompt-arguments-submit")
        await app.workers.wait_for_complete()
        assert "MCP Prompt /mcp__prompts__welcome" in prompt.text
        assert "Welcome, Ada." in prompt.text
        assert model.requests == []


async def test_session_metadata_and_exact_fork_commands_use_session_services(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="answer")])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("question")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        source_id = app.current_session.session_id
        fork_point = app.current_session.leaf_id
        assert fork_point is not None

        prompt.load_text("/session")
        await pilot.press("enter")
        await pilot.pause()
        metadata = str(app.screen.query_one("#info-content").render())
        assert source_id in metadata
        assert fork_point in metadata
        assert "model-a" in metadata
        await pilot.press("escape")
        await app.workers.wait_for_complete()

        prompt.load_text(f"/fork {fork_point}")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        assert app.current_session.session_id != source_id
        assert app.current_session.metadata.parent_session_id == source_id
        assert app.current_session.metadata.fork_point_entry_id == fork_point


async def test_permissions_command_changes_shared_policy_for_following_run(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-denied",
                        "write",
                        {"path": "denied.txt", "content": "no"},
                    )
                ]
            ),
            ModelResponse(content="handled denial"),
        ]
    )
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        approval_mode=DEFAULT_MODE,
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("/permissions plan")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert "Approval plan" in str(app.query_one("#status-bar").render())

        prompt.load_text("try to write")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert not (workspace / "denied.txt").exists()
        assert "approval_denied: write" in str(app.query_one(".tool-call-content").render())


async def test_selector_fallbacks_resume_model_and_permissions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        ScriptedModel([]),
        (ModelInfo("model-a", 100_000), ModelInfo("model-b", 50_000)),
        approval_mode=DEFAULT_MODE,
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        original_id = app.current_session.session_id
        prompt = app.query_one("#prompt", TextArea)

        prompt.load_text("/name Original")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        prompt.load_text("/new")
        await pilot.press("enter")
        await app.workers.wait_for_complete()

        prompt.load_text("/resume")
        await pilot.press("enter")
        await pilot.pause()
        assert "Original" in str(app.screen.query_one("#selection-1").render())
        await pilot.click("#selection-1")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        assert app.current_session.session_id == original_id

        prompt.load_text("/model")
        await pilot.press("enter")
        await pilot.pause()
        assert "model-b" in str(app.screen.query_one("#selection-1").render())
        await pilot.click("#selection-1")
        await app.workers.wait_for_complete()
        assert app.current_session.metadata.model == "model-b"

        prompt.load_text("/permissions")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#selection-2")
        await app.workers.wait_for_complete()
        assert "Approval plan" in str(app.query_one("#status-bar").render())


async def test_history_and_leaf_selectors_use_durable_session_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(content="main answer"),
            ModelResponse(content="alternate answer"),
        ]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("question")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        source_id = app.current_session.session_id
        storage = SessionStorage(factory.settings.session_dir)
        main_view = await materialize_conversation(storage, app.current_session)
        user_entry_id = main_view.entries[0].id
        main_leaf = main_view.entries[-1].id

        prompt.load_text(f"/tree {user_entry_id}")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        prompt.load_text("alternate")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert app.current_session.leaf_id != main_leaf

        prompt.load_text("/tree")
        await pilot.press("enter")
        await pilot.pause()
        assert main_leaf in str(app.screen.query_one("#selection-0").render())
        await pilot.click("#selection-0")
        await app.workers.wait_for_complete()
        assert app.current_session.leaf_id == main_leaf

        prompt.load_text("/fork")
        await pilot.press("enter")
        await pilot.pause()
        assert "question" in str(app.screen.query_one("#selection-0").render())
        await pilot.click("#selection-0")
        await app.workers.wait_for_complete()
        assert app.current_session.session_id != source_id
        assert app.current_session.metadata.parent_session_id == source_id
        assert app.current_session.metadata.fork_point_entry_id == user_entry_id


async def test_manual_compaction_routes_focus_and_renders_structural_marker(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        api_key=SecretStr("test-key"),
        base_url="https://proxy.example/v1",
        default_model="model-a",
        session_dir=tmp_path / "sessions",
        compaction_keep_recent_tokens=0,
        compaction_summary_max_tokens=100,
    )
    model = ScriptedModel(
        [
            ModelResponse(content="answer one"),
            ModelResponse(content="answer two"),
            ModelResponse(content="Earlier decisions were summarized."),
        ]
    )
    factory = TuiRuntimeFactory(tmp_path, settings, model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        for text in ("one", "two"):
            prompt.load_text(text)
            await pilot.press("enter")
            await app.workers.wait_for_complete()

        prompt.load_text("/compact preserve decisions")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert "preserve decisions" in model.requests[-1].messages[-1]["content"]
        assert "Earlier decisions" in str(app.query_one(".compaction-entry").render())
        assert app.current_session is not None
        compacted = await materialize_conversation(
            SessionStorage(settings.session_dir),
            app.current_session,
        )
        assert compacted.dropped_summary == "Earlier decisions were summarized."

        request_count = len(model.requests)
        prompt.load_text("/context")
        await pilot.press("enter")
        await pilot.pause()
        overview = str(app.screen.query_one("#context-overview-content").render())
        assert "Generated dropped-history summary: included" in overview
        await pilot.press("2")
        await pilot.pause()
        await pilot.press(
            "shift+left",
            "shift+down",
            "shift+down",
            "shift+down",
            "down",
        )
        detail = str(app.screen.query_one("#context-content-detail").render())
        assert "Generated dropped-history summary" in detail
        assert "Generated · included" in detail
        assert "Earlier decisions were summarized." in detail
        await pilot.press("3")
        await pilot.pause()
        raw = str(app.screen.query_one("#context-raw-request").render())
        assert "Dropped conversation history summary" in raw
        assert "Earlier decisions were summarized." in raw
        assert len(model.requests) == request_count
        await pilot.press("escape")


async def test_disabled_manual_compaction_is_reported_explicitly(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        api_key=SecretStr("test-key"),
        base_url="https://proxy.example/v1",
        default_model="model-a",
        session_dir=tmp_path / "sessions",
        compaction_enabled=False,
    )
    model = ScriptedModel([])
    factory = TuiRuntimeFactory(tmp_path, settings, model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("/compact")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert "Context compaction is disabled" in str(app.query_one(".run-status").render())
        assert model.requests == []


async def test_escape_cancels_only_active_run_then_drains_ordinary_queue(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = GatedModel([ModelResponse(content="queued answer")])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("cancel me")
        await pilot.press("enter")
        await model.started.wait()
        prompt.load_text("keep me")
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("escape")
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        view = await materialize_conversation(
            SessionStorage(factory.settings.session_dir),
            app.current_session,
        )
        assert [entry.content for entry in view.entries if entry.entry_type == "user_message"] == [
            "cancel me",
            "keep me",
        ]
        statuses = [str(status.render()) for status in app.query(".run-status")]
        assert any("cancelled" in status for status in statuses)
        assert any("completed" in status for status in statuses)


async def test_escape_before_run_start_persists_cancelled_message_and_drains_queue(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = GatedLoadStorage(tmp_path / "sessions")
    model = ScriptedModel([ModelResponse(content="queued answer")])
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        storage=storage,
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        storage.arm()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("cancel before start")
        await pilot.press("enter")
        await storage.started.wait()
        prompt.load_text("still run this")
        await pilot.press("enter")

        await pilot.press("escape")
        await pilot.pause()
        assert app.current_session is not None
        assert app.current_session.revision == 0

        storage.release.set()
        await app.workers.wait_for_complete()
        view = await materialize_conversation(storage, app.current_session)
        assert [entry.content for entry in view.entries if entry.entry_type == "user_message"] == [
            "cancel before start",
            "still run this",
        ]
        statuses = [str(status.render()) for status in app.query(".run-status")]
        assert any("cancelled" in status for status in statuses)
        assert any("completed" in status for status in statuses)


async def test_session_topology_command_cannot_race_active_run(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = GatedModel([ModelResponse(content="answer")])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        session_id = app.current_session.session_id
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("active")
        await pilot.press("enter")
        await model.started.wait()

        prompt.load_text("/new")
        await pilot.press("enter")
        await pilot.pause()
        assert app.current_session.session_id == session_id
        assert list(app.query(".queued-message")) == []
        assert "requires the current Run" in str(list(app.query(".run-status"))[-1].render())

        model.release.set()
        await app.workers.wait_for_complete()


async def test_steer_is_injected_once_at_next_step_without_becoming_an_entry(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("ground truth")
    model = GatedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read-1", "read", {"path": "input.txt"})]),
            ModelResponse(content="steered answer"),
        ]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("initial")
        await pilot.press("enter")
        await model.started.wait()
        prompt.load_text("redirect now")
        await pilot.press("enter")
        await pilot.pause()
        toggle_id = next(
            button.id
            for button in app.query(".queue-row Button")
            if button.id is not None and button.id.startswith("toggle-")
        )
        await pilot.click(f"#{toggle_id}")

        model.release.set()
        await app.workers.wait_for_complete()
        assert {"role": "user", "content": "redirect now"} in model.requests[1].messages
        assert app.current_session is not None
        view = await materialize_conversation(
            SessionStorage(factory.settings.session_dir),
            app.current_session,
        )
        assert [entry.content for entry in view.entries if entry.entry_type == "user_message"] == [
            "initial"
        ]


async def test_unconsumed_steer_remains_pending_and_is_not_sent_in_a_later_run(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = GatedModel(
        [
            ModelResponse(content="first answer"),
            ModelResponse(content="queued answer"),
        ]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("initial")
        await pilot.press("enter")
        await model.started.wait()

        prompt.load_text("too late to steer")
        await pilot.press("enter")
        await pilot.pause()
        toggle_id = next(
            button.id
            for button in app.query(".queue-row Button")
            if button.id is not None and button.id.startswith("toggle-")
        )
        await pilot.click(f"#{toggle_id}")
        prompt.load_text("ordinary queue")
        await pilot.click("#prompt")
        await pilot.press("enter")

        model.release.set()
        await app.workers.wait_for_complete()
        assert len(model.requests) == 2
        assert {"role": "user", "content": "too late to steer"} not in model.requests[1].messages
        assert model.requests[1].messages[-1] == {
            "role": "user",
            "content": "ordinary queue",
        }
        assert app.current_session is not None
        view = await materialize_conversation(
            SessionStorage(factory.settings.session_dir),
            app.current_session,
        )
        assert [entry.content for entry in view.entries if entry.entry_type == "user_message"] == [
            "initial",
            "ordinary queue",
        ]
        pending = list(app.query(".queued-message"))
        assert len(pending) == 1
        assert "steer" in str(pending[0].render())
        assert "too late to steer" in str(pending[0].render())


async def test_partial_tool_json_stays_hidden_and_reasoning_is_collapsed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("ground truth")
    model = FragmentedToolModel()
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("inspect")
        await pilot.press("enter")
        await model.fragment_seen.wait()
        await pilot.pause()

        assert list(app.query(".tool-call")) == []
        reasoning = app.query_one(".reasoning-message", Collapsible)
        assert reasoning.collapsed is True
        assert "checking the file" in str(
            reasoning.query_one(".reasoning-content", Static).render()
        )
        assert "checking the file" not in app.query_one(".assistant-message", Markdown).source

        model.release.set()
        await app.workers.wait_for_complete()
        cards = list(app.query(".tool-call"))
        assert len(cards) == 1
        card_content = str(cards[0].query_one(".tool-call-content").render())
        assert "complete" in card_content
        assert "input.txt" in card_content
        assert "ground truth" in card_content


async def test_multiline_submission_and_whitespace_rejection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="answer")])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        revision = app.current_session.revision
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("  \n ")
        await pilot.press("enter")
        await pilot.pause()
        assert app.current_session.revision == revision
        assert model.requests == []

        prompt.load_text("first line")
        prompt.cursor_location = (0, len("first line"))
        await pilot.press("shift+enter")
        await pilot.press("s", "e", "c", "o", "n", "d")
        assert prompt.text == "first line\nsecond"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert model.requests[0].messages[-1] == {
            "role": "user",
            "content": "first line\nsecond",
        }


async def test_queue_edit_and_remove_do_not_persist_discarded_drafts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = GatedModel(
        [ModelResponse(content="first answer"), ModelResponse(content="edited answer")]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("first")
        await pilot.press("enter")
        await model.started.wait()

        prompt.load_text("second")
        await pilot.press("enter")
        await pilot.pause()
        edit_id = next(
            button.id
            for button in app.query(".queue-row Button")
            if button.id is not None and button.id.startswith("edit-")
        )
        await pilot.click(f"#{edit_id}")
        assert prompt.text == "second"
        prompt.load_text("edited second")
        await pilot.press("enter")

        prompt.load_text("discard me")
        await pilot.press("enter")
        await pilot.pause()
        discard_row = next(
            row
            for row in app.query(".queue-row")
            if "discard me" in str(row.query_one(".queued-message").render())
        )
        remove_id = next(
            button.id
            for button in discard_row.query("Button")
            if button.id is not None and button.id.startswith("remove-")
        )
        await pilot.click(f"#{remove_id}")

        model.release.set()
        await app.workers.wait_for_complete()
        assert app.current_session is not None
        view = await materialize_conversation(
            SessionStorage(factory.settings.session_dir),
            app.current_session,
        )
        assert [entry.content for entry in view.entries if entry.entry_type == "user_message"] == [
            "first",
            "edited second",
        ]


async def test_failed_run_is_redacted_and_does_not_strand_queue(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "sk-abcdefghijk"
    model = GatedModel(
        [RuntimeError(f"provider rejected {secret}"), ModelResponse(content="recovered")]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("fail")
        await pilot.press("enter")
        await model.started.wait()
        prompt.load_text("recover")
        await pilot.press("enter")
        model.release.set()
        await app.workers.wait_for_complete()

        statuses = "\n".join(str(status.render()) for status in app.query(".run-status"))
        assert secret not in statuses
        assert "[REDACTED]" in statuses
        assert "recovered" in app.query_one(".assistant-message", Markdown).source


async def test_pre_persistence_failure_removes_optimistic_message_and_restores_draft(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = FailingAppendStorage(tmp_path / "sessions")
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        ScriptedModel([]),
        storage=storage,
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("retry this message")
        await pilot.press("enter")
        await app.workers.wait_for_complete()

        assert app.current_session is not None
        assert app.current_session.revision == 0
        assert list(app.query(".user-message")) == []
        assert prompt.text == "retry this message"
        assert "simulated append failure" in str(list(app.query(".run-status"))[-1].render())


async def test_tool_error_is_redacted_in_ui_but_remains_durable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "sk-abcdefghijk"

    @tool(name="leaky", description="Raise a credential-shaped failure.")
    async def leaky() -> str:
        raise RuntimeError(f"service rejected {secret}")

    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("leaky-1", "leaky", {})]),
            ModelResponse(content="recovered"),
        ]
    )
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        extra_tools=(leaky,),
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("use the tool")
        await pilot.press("enter")
        await app.workers.wait_for_complete()

        card = app.query_one(".tool-call")
        content = str(card.query_one(".tool-call-content").render())
        assert "Arguments: {}" in content
        assert secret not in content
        assert "[REDACTED]" in content
        assert app.current_session is not None
        view = await materialize_conversation(
            SessionStorage(factory.settings.session_dir),
            app.current_session,
        )
        tool_result = next(entry for entry in view.entries if entry.entry_type == "tool_result")
        assert tool_result.result.error is not None
        assert secret in tool_result.result.error


async def test_max_step_run_does_not_strand_queue(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("ground truth")
    model = GatedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read-max", "read", {"path": "input.txt"})]),
            ModelResponse(content="next run"),
        ]
    )
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory, max_steps=1)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("exhaust")
        await pilot.press("enter")
        await model.started.wait()
        prompt.load_text("after exhaustion")
        await pilot.press("enter")
        model.release.set()
        await app.workers.wait_for_complete()

        statuses = [str(status.render()) for status in app.query(".run-status")]
        assert any("exhausted" in status for status in statuses)
        assert any("completed" in status for status in statuses)
        assert list(app.query(".queued-message")) == []


async def test_allow_for_session_reuses_tool_name_authority_in_memory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("write-1", "write", {"path": "one.txt", "content": "one"})]
            ),
            ModelResponse(content="first done"),
            ModelResponse(
                tool_calls=[ToolCall("write-2", "write", {"path": "two.txt", "content": "two"})]
            ),
            ModelResponse(content="second done"),
        ]
    )
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        approval_mode=DEFAULT_MODE,
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("first write")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#approval-session")
        await app.workers.wait_for_complete()

        prompt.load_text("second write")
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert (workspace / "one.txt").read_text() == "one"
        assert (workspace / "two.txt").read_text() == "two"


async def test_reopening_session_renders_durable_tool_result_and_reasoning(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("persisted result")
    first_factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[ToolCall("read-persisted", "read", {"path": "input.txt"})]
                ),
                ModelResponse(content="durable answer", reasoning="durable reasoning"),
            ]
        ),
    )
    first_app = PhiApp(cwd=workspace, runtime_factory=first_factory)

    async with first_app.run_test() as pilot:
        await pilot.pause()
        prompt = first_app.query_one("#prompt", TextArea)
        prompt.load_text("remember this")
        await pilot.press("enter")
        await first_app.workers.wait_for_complete()
        assert first_app.current_session is not None
        persisted = first_app.current_session

    second_factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), ScriptedModel([]))
    second_app = PhiApp(
        initial_session=persisted,
        cwd=workspace,
        runtime_factory=second_factory,
    )
    async with second_app.run_test() as pilot:
        await pilot.pause()
        assert "remember this" in str(second_app.query_one(".user-message").render())
        assert "durable answer" in second_app.query_one(".assistant-message", Markdown).source
        reasoning = second_app.query_one(".reasoning-message", Collapsible)
        assert reasoning.collapsed is True
        tool_card = second_app.query_one(".tool-call")
        tool_content = str(tool_card.query_one(".tool-call-content").render())
        assert "complete" in tool_content
        assert "persisted result" in tool_content

    assert first_factory.close_count == 1
    assert second_factory.close_count == 1


async def test_quit_cancels_active_run_discards_queue_and_closes_once(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = GatedModel([ModelResponse(content="must not complete")])
    factory = TuiRuntimeFactory(tmp_path, _settings(tmp_path), model)
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_session is not None
        session_id = app.current_session.session_id
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("active")
        await pilot.press("enter")
        await model.started.wait()
        prompt.load_text("queued but not sent")
        await pilot.press("enter")
        prompt.load_text("/quit")
        await pilot.press("enter")
        await asyncio.wait_for(model.cancelled.wait(), timeout=1)

    assert factory.close_count == 1
    storage = SessionStorage(factory.settings.session_dir)
    view = await materialize_conversation(
        storage,
        await resume_session(storage, session_id),
    )
    assert [entry.content for entry in view.entries if entry.entry_type == "user_message"] == [
        "active"
    ]


async def test_quit_while_approval_waits_denies_by_cancellation_and_closes_once(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-waiting",
                        "write",
                        {"path": "never.txt", "content": "never"},
                    )
                ]
            )
        ]
    )
    factory = TuiRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        approval_mode=DEFAULT_MODE,
    )
    app = PhiApp(cwd=workspace, runtime_factory=factory)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", TextArea)
        prompt.load_text("wait for approval")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#approval-tool") is not None
        await pilot.press("ctrl+q")

    assert factory.close_count == 1
    assert not (workspace / "never.txt").exists()
