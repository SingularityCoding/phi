from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

from pydantic import SecretStr
from typer.testing import CliRunner

from phi.bootstrap import HostRuntime, build_runtime_resources
from phi.cli import app
from phi.harness import RunStatus
from phi.instructions import PHI_BASE_INSTRUCTIONS
from phi.mcp import McpConfig, McpServerConfig, load_mcp_config, save_mcp_config
from phi.model import (
    ModelHTTPError,
    ModelInfo,
    ModelProtocolError,
    ModelResponse,
    ScriptedModel,
    ToolCall,
    Usage,
)
from phi.sessions import (
    SessionHandle,
    SessionStorage,
    create_session,
    fork_session,
    manual_compact,
    rename_session,
    send_message,
)
from phi.settings import Settings
from phi.tools import BYPASS_MODE, RuleBasedApprovalPolicy, ToolDispatcher, ToolRegistry

runner = CliRunner()


def _settings(root: Path, *, default_model: str = "model-a") -> Settings:
    return Settings(
        api_key=SecretStr("test-key"),
        base_url="https://proxy.example/v1",
        default_model=default_model,
        session_dir=root / "sessions",
    )


async def _completed_session(storage: SessionStorage):
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a", name="Original")
    handle, result = await send_message(
        handle,
        "question",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="answer")]),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )
    assert result.status is RunStatus.COMPLETED
    assert handle.leaf_id is not None
    return handle


@dataclass
class ContextRuntimeFactory:
    root: Path
    settings: Settings
    available_models: tuple[ModelInfo, ...] = (ModelInfo("model-a", 100_000),)
    model: ScriptedModel | None = None
    interrupt_on_resume: bool = False
    close_count: int = 0

    async def __call__(self, cwd: Path) -> HostRuntime:
        resources = await build_runtime_resources(
            cwd,
            base_instructions=PHI_BASE_INSTRUCTIONS,
            approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
            global_skill_root=self.root / "global-skills",
            global_agent_root=self.root / "global-agents",
            global_mcp_config_path=self.root / "global-mcp.json",
        )

        async def observe_close() -> None:
            self.close_count += 1

        return HostRuntime(
            settings=self.settings,
            model=self.model or ScriptedModel([]),
            available_models=self.available_models,
            storage=(
                InterruptingSessionStorage(self.settings.session_dir)
                if self.interrupt_on_resume
                else SessionStorage(self.settings.session_dir)
            ),
            resources=resources,
            close_callback=observe_close,
        )


class InterruptingSessionStorage(SessionStorage):
    async def load_state(self, session_id: str) -> Never:
        del session_id
        raise KeyboardInterrupt


def test_session_list_reports_empty_store(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    result = runner.invoke(app, ["session", "list"])

    assert result.exit_code == 0
    assert "Sessions (0)" in result.stdout
    assert "No Sessions found." in result.stdout
    assert "\x1b[" not in result.stdout
    assert result.stderr == ""


def test_session_list_orders_latest_first_and_reports_recovery_diagnostics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)

    async def arrange():
        latest = await create_session(storage, model="model-a", name="Latest")
        older = await create_session(storage, model=None)
        latest = await rename_session(storage, latest, "Latest renamed")
        return latest, older

    latest, older = asyncio.run(arrange())
    storage.journal_path(latest.session_id).write_bytes(
        b'{"schema_version":1,"entry_type":"user_message"'
    )
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    result = runner.invoke(app, ["session", "list"])

    assert result.exit_code == 0
    assert "Sessions (2)" in result.stdout
    assert all(
        label in result.stdout for label in ("ID", "Name", "Model", "Updated", "Origin", "Leaf")
    )
    assert result.stdout.index(latest.session_id) < result.stdout.index(older.session_id)
    assert "Latest renamed" in result.stdout
    assert "model-a" in result.stdout
    assert "new" in result.stdout
    assert "\x1b[" not in result.stdout
    assert result.stderr.count("ignored 1 uncommitted trailing Entry record(s)") == 1


def test_session_list_uses_id_ties_and_shows_fork_and_subagent_origins(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)

    async def arrange() -> tuple[str, ...]:
        root = await _completed_session(storage)
        assert root.leaf_id is not None
        forked = await fork_session(storage, root, root.leaf_id)
        subagent = await create_session(
            storage,
            model="model-a",
            origin="subagent",
            parent_session_id=root.session_id,
        )
        tied_at = datetime(2026, 1, 2, tzinfo=UTC)
        for handle in (root, forked, subagent):
            state = await storage.load_state(handle.session_id)
            await storage.replace_metadata(
                handle.session_id,
                expected_revision=state.envelope.revision,
                metadata=state.envelope.metadata.model_copy(update={"updated_at": tied_at}),
            )
        return root.session_id, forked.session_id, subagent.session_id

    session_ids = asyncio.run(arrange())
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    result = runner.invoke(app, ["session", "list"])

    assert result.exit_code == 0
    ordered_ids = sorted(session_ids, reverse=True)
    positions = [result.stdout.index(session_id) for session_id in ordered_ids]
    assert positions == sorted(positions)
    assert all(origin in result.stdout for origin in ("new", "fork", "subagent"))


def test_session_list_fails_closed_on_corrupt_data(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    handle = asyncio.run(create_session(storage))
    storage.metadata_path(handle.session_id).write_text("not-json", encoding="utf-8")
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    result = runner.invoke(app, ["session", "list"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "is corrupt" in result.stderr
    assert "Traceback" not in result.stderr


def test_session_resume_hands_validated_handle_and_cwd_to_tui(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path)
    handle = asyncio.run(create_session(SessionStorage(settings.session_dir), model="model-a"))
    launches: list[tuple[SessionHandle, Path]] = []
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr(
        "phi.cli.main.run_tui",
        lambda *, initial_session, cwd: launches.append((initial_session, cwd)),
    )

    result = runner.invoke(app, ["session", "resume", handle.session_id])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    resumed, cwd = launches[0]
    assert resumed.session_id == handle.session_id
    assert cwd == workspace


def test_session_resume_rejects_missing_session_without_launch(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    launches: list[object] = []
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main.run_tui", lambda **kwargs: launches.append(kwargs))

    result = runner.invoke(app, ["session", "resume", "missing"])

    assert result.exit_code == 1
    assert launches == []
    assert "was not found" in result.stderr


def test_operational_error_styles_markup_shaped_text_without_interpreting_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    missing_id = "[bold red]missing[/bold red]"
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.rendering._stream_is_terminal", lambda stream: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    result = runner.invoke(
        app,
        ["session", "resume", missing_id],
        color=True,
    )

    assert result.exit_code == 1
    assert missing_id in result.stderr
    assert "\x1b[" in result.stderr
    assert "Traceback" not in result.stderr


def test_session_fork_inherits_model_and_reports_lineage(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    source = asyncio.run(_completed_session(storage))
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    result = runner.invoke(
        app,
        ["session", "fork", source.session_id, source.leaf_id],
    )

    assert result.exit_code == 0
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    fork_state = asyncio.run(storage.load_state(values["session_id"]))
    assert values["parent_session_id"] == source.session_id
    assert values["fork_point_entry_id"] == source.leaf_id
    assert fork_state.envelope.metadata.model == "model-a"
    assert fork_state.envelope.metadata.origin == "fork"
    assert fork_state.entries == ()
    assert len(asyncio.run(storage.list_metadata())) == 2


def test_session_fork_renders_clear_lineage_in_an_interactive_terminal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    source = asyncio.run(_completed_session(storage))
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.rendering._stream_is_terminal", lambda stream: True)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    result = runner.invoke(
        app,
        ["session", "fork", source.session_id, source.leaf_id],
        color=True,
    )

    assert result.exit_code == 0
    assert "Fork created" in result.stdout
    assert "New Session" in result.stdout
    assert "Parent Session" in result.stdout
    assert "Fork point" in result.stdout
    assert source.session_id in result.stdout
    assert source.leaf_id in result.stdout
    assert "\x1b[" in result.stdout


def test_session_fork_honors_no_color_and_wraps_safely_at_narrow_width(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    source = asyncio.run(_completed_session(storage))
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.rendering._stream_is_terminal", lambda stream: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("NO_COLOR", "1")

    result = runner.invoke(
        app,
        ["session", "fork", source.session_id, source.leaf_id],
        color=True,
    )

    assert result.exit_code == 0
    assert "Fork created" in result.stdout
    assert "New Session" in result.stdout
    assert "\x1b[" not in result.stdout
    compact = "".join(result.stdout.replace("│", "").split())
    assert source.session_id in compact
    assert source.leaf_id in compact


def test_session_fork_validates_explicit_model_before_persisting(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    source = asyncio.run(_completed_session(storage))
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    async def discover_models(config):
        del config
        return [ModelInfo("model-a"), ModelInfo("model-b")]

    monkeypatch.setattr("phi.cli.main._model_discovery", discover_models)

    available = runner.invoke(
        app,
        ["session", "fork", source.session_id, source.leaf_id, "--model", "model-b"],
    )
    before_failure = len(asyncio.run(storage.list_metadata()))
    unavailable = runner.invoke(
        app,
        ["session", "fork", source.session_id, source.leaf_id, "--model", "missing"],
    )
    blank = runner.invoke(
        app,
        ["session", "fork", source.session_id, source.leaf_id, "--model", "   "],
    )

    available_id = dict(line.split("=", 1) for line in available.stdout.splitlines())["session_id"]
    assert available.exit_code == 0
    assert asyncio.run(storage.load(available_id)).metadata.model == "model-b"
    assert unavailable.exit_code == 1
    assert "Model 'missing' is not available" in unavailable.stderr
    assert blank.exit_code == 1
    assert "--model must contain non-whitespace text" in blank.stderr
    assert len(asyncio.run(storage.list_metadata())) == before_failure


def test_session_fork_redacts_credential_shaped_unavailable_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    source = asyncio.run(_completed_session(storage))
    secret_model = "sk-fake-secret-for-redaction"
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    async def discover_models(config):
        del config
        return [ModelInfo("model-a")]

    monkeypatch.setattr("phi.cli.main._model_discovery", discover_models)

    result = runner.invoke(
        app,
        ["session", "fork", source.session_id, source.leaf_id, "--model", secret_model],
    )

    assert result.exit_code == 1
    assert secret_model not in result.stderr
    assert "[REDACTED]" in result.stderr
    assert len(asyncio.run(storage.list_metadata())) == 1


def test_session_fork_rejects_entry_outside_selected_view(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    source = asyncio.run(_completed_session(storage))
    unrelated = asyncio.run(_completed_session(storage))
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)

    result = runner.invoke(
        app,
        ["session", "fork", source.session_id, unrelated.leaf_id],
    )

    assert result.exit_code == 1
    assert "invalid leaf" in result.stderr
    assert len(asyncio.run(storage.list_metadata())) == 2


def test_context_empty_store_fails_without_building_runtime_or_creating_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    factory_called = False

    async def unexpected_factory(cwd: Path) -> HostRuntime:
        del cwd
        nonlocal factory_called
        factory_called = True
        raise AssertionError("empty Context inspection must not build a full runtime")

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", unexpected_factory)

    result = runner.invoke(app, ["context", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "No Sessions found" in result.stderr
    assert factory_called is False
    assert list(settings.session_dir.glob("*.metadata.json")) == []


def test_context_json_selects_latest_session_and_is_complete_and_machine_pure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)

    async def arrange():
        older = await create_session(storage, model="model-a", name="Older")
        latest = await _completed_session(storage)
        latest = await rename_session(storage, latest, "Latest")
        return older, latest

    older, latest = asyncio.run(arrange())
    with storage.journal_path(latest.session_id).open("ab") as file:
        file.write(b'{"schema_version":1,"entry_type":"user_message"')
    model = ScriptedModel([])
    factory = ContextRuntimeFactory(tmp_path, settings, model=model)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--json"])

    document = json.loads(result.stdout)
    assert result.exit_code == 0
    assert document["schema_version"] == 1
    assert document["session"]["id"] == latest.session_id
    assert document["session"]["name"] == "Latest"
    assert document["model"] == "model-a"
    assert document["context"]["system_prompt"]
    assert document["context"]["tools"]
    assert document["context"]["messages"] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert document["model_request"]["messages"][-2:] == document["context"]["messages"]
    assert set(document["character_counts"]) == {
        "system_prompt",
        "dropped_summary",
        "messages",
        "tools",
    }
    assert document["token_estimate"]["tokens"] > 0
    assert document["token_estimate"]["local_tokens"] > 0
    assert document["token_estimate"]["used_provider_anchor"] is False
    assert document["input_limits"] == {"effective": 100_000, "safe": 83_616}
    assert document["diagnostics"] == ["ignored 1 uncommitted trailing Entry record(s)"]
    assert result.stderr.count("ignored 1 uncommitted trailing Entry record(s)") == 1
    assert "\x1b[" not in result.stdout
    assert "\x1b[" not in result.stderr
    assert older.session_id not in result.stdout
    assert model.requests == []
    assert factory.close_count == 1


def test_context_explicit_session_wins_and_plain_output_labels_complete_sections(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)
    selected = asyncio.run(_completed_session(storage))
    asyncio.run(rename_session(storage, selected, "Selected"))
    asyncio.run(create_session(storage, model="model-a", name="Newer"))
    factory = ContextRuntimeFactory(tmp_path, settings)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--session", selected.session_id])

    assert result.exit_code == 0
    assert "Overview" in result.stdout
    assert f"Session ID: {selected.session_id}" in result.stdout
    assert "Model: model-a" in result.stdout
    assert "System prompt" in result.stdout
    assert "Tools" in result.stdout
    assert "Messages" in result.stdout
    assert "Message 1 · User" in result.stdout
    assert "Message 2 · Assistant" in result.stdout
    assert '"role": "user"' in result.stdout
    assert "Dropped-history summary" in result.stdout
    assert "Character counts" in result.stdout
    assert "Token Estimate:" in result.stdout
    assert "Provider Usage anchor: no" in result.stdout
    assert "Effective input limit: 100000" in result.stdout
    assert "Safe input limit: 83616" in result.stdout
    assert "Usage:" not in result.stdout
    assert factory.close_count == 1


def test_context_rejects_unavailable_effective_model_and_closes_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path)
    handle = asyncio.run(create_session(SessionStorage(settings.session_dir), model="unavailable"))
    factory = ContextRuntimeFactory(tmp_path, settings)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--session", handle.session_id, "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Model 'unavailable' is not available" in result.stderr
    assert factory.close_count == 1


def test_context_closes_runtime_once_when_interrupted(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path)
    handle = asyncio.run(create_session(SessionStorage(settings.session_dir), model="model-a"))
    factory = ContextRuntimeFactory(tmp_path, settings, interrupt_on_resume=True)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--session", handle.session_id])

    assert result.exit_code == 130
    assert result.stderr == "Operation cancelled\n"
    assert factory.close_count == 1


def test_context_plain_output_distinguishes_tool_calls_and_results(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)

    async def arrange():
        tools = ToolRegistry()
        handle = await create_session(storage, model="model-a")
        handle, _ = await send_message(
            handle,
            "use a tool",
            storage=storage,
            settings=settings,
            model=ScriptedModel(
                [
                    ModelResponse(tool_calls=[ToolCall("call-1", "missing", {"value": 1})]),
                    ModelResponse(content="recovered"),
                ]
            ),
            model_info=None,
            tools=tools,
            dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
            stable_instructions="stable",
            max_steps=2,
        )
        return handle

    handle = asyncio.run(arrange())
    factory = ContextRuntimeFactory(tmp_path, settings)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--session", handle.session_id])

    assert result.exit_code == 0
    assert "Assistant · Tool Calls" in result.stdout
    assert "Tool Result" in result.stdout
    assert '"tool_call_id": "call-1"' in result.stdout
    assert '"content": "unknown_tool: missing"' in result.stdout


def test_context_materializes_fork_ancestry_and_branch_model_without_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path, default_model="model-b")
    storage = SessionStorage(settings.session_dir)

    async def arrange() -> SessionHandle:
        root = await _completed_session(storage)
        assert root.leaf_id is not None
        forked = await fork_session(storage, root, root.leaf_id)
        tools = ToolRegistry()
        forked, result = await send_message(
            forked,
            "fork question",
            storage=storage,
            settings=settings,
            model=ScriptedModel(
                [ModelResponse(content="fork answer", reasoning="private reasoning")]
            ),
            model_info=None,
            tools=tools,
            dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
            stable_instructions="stable",
            max_steps=1,
        )
        assert result.status is RunStatus.COMPLETED
        return forked

    forked = asyncio.run(arrange())
    before = {
        path.name: path.read_bytes() for path in settings.session_dir.iterdir() if path.is_file()
    }
    factory = ContextRuntimeFactory(
        tmp_path,
        settings,
        available_models=(ModelInfo("model-a", 100_000), ModelInfo("model-b", 100_000)),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--session", forked.session_id, "--json"])

    document = json.loads(result.stdout)
    after = {
        path.name: path.read_bytes() for path in settings.session_dir.iterdir() if path.is_file()
    }
    assert result.exit_code == 0
    assert document["model"] == "model-a"
    assert document["session"]["origin"] == "fork"
    assert document["session"]["parent_session_id"] is not None
    assert document["context"]["messages"] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "fork question"},
        {
            "role": "assistant",
            "content": "fork answer",
            "reasoning_content": "private reasoning",
        },
    ]
    assert before == after
    assert factory.close_count == 1


def test_context_renders_compaction_summary_without_model_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path)
    storage = SessionStorage(settings.session_dir)

    async def arrange() -> SessionHandle:
        tools = ToolRegistry()
        dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
        handle = await create_session(storage, model="model-a")
        for prompt, answer in (("one", "answer one"), ("two", "answer two")):
            handle, result = await send_message(
                handle,
                prompt,
                storage=storage,
                settings=settings,
                model=ScriptedModel(
                    [
                        ModelResponse(
                            content=answer,
                            usage=Usage(
                                prompt_tokens=25,
                                completion_tokens=2,
                                total_tokens=27,
                            ),
                        )
                    ]
                ),
                model_info=None,
                tools=tools,
                dispatcher=dispatcher,
                stable_instructions="stable",
                max_steps=1,
            )
            assert result.status is RunStatus.COMPLETED
        return await manual_compact(
            handle,
            storage=storage,
            settings=settings.model_copy(update={"compaction_keep_recent_tokens": 0}),
            model=ScriptedModel([ModelResponse(content="Earlier discussion summary.")]),
            model_info=ModelInfo("model-a", max_output_tokens=100),
            tools=tools,
            stable_instructions="stable",
        )

    compacted = asyncio.run(arrange())
    inspection_model = ScriptedModel([])
    factory = ContextRuntimeFactory(tmp_path, settings, model=inspection_model)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--session", compacted.session_id, "--json"])

    document = json.loads(result.stdout)
    assert result.exit_code == 0
    assert document["context"]["dropped_summary"] == "Earlier discussion summary."
    assert document["context"]["messages"] == [
        {"role": "assistant", "content": "answer two"},
    ]
    assert document["model_request"]["messages"][1] == {
        "role": "system",
        "content": "Dropped conversation history summary:\nEarlier discussion summary.",
    }
    assert inspection_model.requests == []
    assert factory.close_count == 1


def test_context_latest_tie_uses_id_default_model_unknown_limits_and_no_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = _settings(tmp_path, default_model="model-b")
    storage = SessionStorage(settings.session_dir)

    async def arrange() -> tuple[str, str]:
        first = await create_session(storage)
        second = await create_session(storage)
        tied_at = datetime(2026, 1, 2, tzinfo=UTC)
        for handle in (first, second):
            await storage.replace_metadata(
                handle.session_id,
                expected_revision=handle.revision,
                metadata=handle.metadata.model_copy(update={"updated_at": tied_at}),
            )
        return first.session_id, second.session_id

    session_ids = asyncio.run(arrange())
    before = {
        path.name: path.read_bytes() for path in settings.session_dir.iterdir() if path.is_file()
    }
    factory = ContextRuntimeFactory(
        tmp_path,
        settings,
        available_models=(ModelInfo("model-b"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["context", "--json"])

    document = json.loads(result.stdout)
    after = {
        path.name: path.read_bytes() for path in settings.session_dir.iterdir() if path.is_file()
    }
    assert result.exit_code == 0
    assert document["session"]["id"] == max(session_ids)
    assert document["model"] == "model-b"
    assert document["input_limits"] == {"effective": None, "safe": None}
    assert document["diagnostics"] == [
        "Model input limit is unknown; proactive Context budgeting is best-effort"
    ]
    assert "Model input limit is unknown" in result.stderr
    assert before == after
    assert factory.close_count == 1


def _mcp_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    global_path = tmp_path / "global" / "mcp.json"
    project_path = tmp_path / "workspace" / ".phi" / "mcp.json"
    monkeypatch.setattr("phi.cli.main._global_mcp_path", lambda: global_path)
    monkeypatch.setattr("phi.cli.main._project_mcp_path", lambda cwd: project_path)
    return global_path, project_path


def test_mcp_add_preserves_literal_arguments_and_rejects_duplicate_without_rewrite(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, project_path = _mcp_paths(monkeypatch, tmp_path)

    added = runner.invoke(
        app,
        ["mcp", "add", "demo", "--", "python", "--global", "--flag=value", "two words"],
    )
    original = project_path.read_bytes()
    duplicate = runner.invoke(app, ["mcp", "add", "demo", "--", "other"])

    config = asyncio.run(load_mcp_config(project_path))
    assert added.exit_code == 0
    assert added.stdout == "Added project MCP server 'demo'.\n"
    assert config.servers["demo"] == McpServerConfig(
        command="python",
        args=("--global", "--flag=value", "two words"),
    )
    assert duplicate.exit_code == 1
    assert "already exists in project MCP configuration" in duplicate.stderr
    assert project_path.read_bytes() == original


def test_mcp_add_global_scope_and_invalid_inputs(monkeypatch, tmp_path: Path) -> None:
    global_path, project_path = _mcp_paths(monkeypatch, tmp_path)

    added = runner.invoke(
        app,
        ["mcp", "add", "--global", "shared", "--", "node", "server.js"],
    )
    invalid_name = runner.invoke(app, ["mcp", "add", "bad name", "--", "server"])
    missing_command = runner.invoke(app, ["mcp", "add", "missing", "--"])

    assert added.exit_code == 0
    assert "global MCP server 'shared'" in added.stdout
    assert asyncio.run(load_mcp_config(global_path)).servers["shared"].args == ("server.js",)
    assert not project_path.exists()
    assert invalid_name.exit_code == 1
    assert "letters, digits, underscores, or hyphens" in invalid_name.stderr
    assert missing_command.exit_code == 2


def test_mcp_mutation_confirmations_are_colored_only_in_interactive_terminals(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _mcp_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("phi.cli.rendering._stream_is_terminal", lambda stream: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    added = runner.invoke(
        app,
        ["mcp", "add", "demo", "--", "server"],
        color=True,
    )
    removed = runner.invoke(app, ["mcp", "remove", "demo"], color=True)

    assert added.exit_code == 0
    assert removed.exit_code == 0
    assert "Added project MCP server 'demo'." in added.stdout
    assert "Removed project MCP server 'demo'." in removed.stdout
    assert "\x1b[" in added.stdout
    assert "\x1b[" in removed.stdout


def test_mcp_list_shows_effective_sources_and_env_names_without_values(
    monkeypatch,
    tmp_path: Path,
) -> None:
    global_path, project_path = _mcp_paths(monkeypatch, tmp_path)
    asyncio.run(
        save_mcp_config(
            global_path,
            McpConfig(
                mcpServers={
                    "global-only": McpServerConfig(
                        command="global",
                        env={"GLOBAL_TOKEN": "global-secret-value"},
                    ),
                    "shared": McpServerConfig(command="global-shared", args=("old",)),
                }
            ),
        )
    )
    asyncio.run(
        save_mcp_config(
            project_path,
            McpConfig(
                mcpServers={
                    "shared": McpServerConfig(
                        command="[bold]project-shared[/bold]",
                        args=("long-" + "x" * 100, "TAIL-MARKER"),
                        env={"PROJECT_SECRET": "project-secret-value"},
                        enabled=False,
                    )
                }
            ),
        )
    )

    effective = runner.invoke(app, ["mcp", "list"])
    global_only = runner.invoke(app, ["mcp", "list", "--global"])

    assert effective.exit_code == 0
    assert "MCP servers (2)" in effective.stdout
    assert "Scope: effective" in effective.stdout
    assert all(
        label in effective.stdout
        for label in ("ID", "Source", "State", "Command", "Arguments", "Environment")
    )
    assert all(
        value in effective.stdout
        for value in (
            "global-only",
            "global",
            "enabled",
            "GLOBAL_TOKEN",
            "shared",
            "project",
            "disabled",
            "[bold]project-shared[/bold]",
            "TAIL-MARKER",
            "PROJECT_SECRET",
        )
    )
    assert "global-shared" not in effective.stdout
    assert "global-secret-value" not in effective.stdout
    assert "project-secret-value" not in effective.stdout
    assert "Scope: global" in global_only.stdout
    assert all(value in global_only.stdout for value in ("shared", "global-shared", "old"))
    assert "global-only" in global_only.stdout


def test_mcp_remove_targets_one_source_and_reveals_global_override(
    monkeypatch,
    tmp_path: Path,
) -> None:
    global_path, project_path = _mcp_paths(monkeypatch, tmp_path)
    asyncio.run(
        save_mcp_config(
            global_path,
            McpConfig(mcpServers={"shared": McpServerConfig(command="global")}),
        )
    )
    asyncio.run(
        save_mcp_config(
            project_path,
            McpConfig(
                mcpServers={
                    "shared": McpServerConfig(command="project"),
                    "keep": McpServerConfig(command="keep"),
                }
            ),
        )
    )

    removed = runner.invoke(app, ["mcp", "remove", "shared"])
    effective = runner.invoke(app, ["mcp", "list"])
    before_missing = project_path.read_bytes()
    missing = runner.invoke(app, ["mcp", "remove", "missing"])

    assert removed.exit_code == 0
    assert removed.stdout == "Removed project MCP server 'shared'.\n"
    assert set(asyncio.run(load_mcp_config(project_path)).servers) == {"keep"}
    assert all(value in effective.stdout for value in ("shared", "global", "enabled"))
    assert missing.exit_code == 1
    assert "does not exist in project MCP configuration" in missing.stderr
    assert project_path.read_bytes() == before_missing


def test_mcp_list_empty_state_is_successful(monkeypatch, tmp_path: Path) -> None:
    _mcp_paths(monkeypatch, tmp_path)

    result = runner.invoke(app, ["mcp", "list"])

    assert result.exit_code == 0
    assert "Scope: effective" in result.stdout
    assert "MCP servers (0)" in result.stdout
    assert "No MCP servers configured." in result.stdout
    assert "\x1b[" not in result.stdout


def test_mcp_configuration_commands_never_start_subprocesses(monkeypatch, tmp_path: Path) -> None:
    _mcp_paths(monkeypatch, tmp_path)
    subprocess_started = False

    async def unexpected_subprocess(*args, **kwargs):
        del args, kwargs
        nonlocal subprocess_started
        subprocess_started = True
        raise AssertionError("MCP configuration commands must not start servers")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_subprocess)

    added = runner.invoke(app, ["mcp", "add", "demo", "--", "server"])
    listed = runner.invoke(app, ["mcp", "list"])
    removed = runner.invoke(app, ["mcp", "remove", "demo"])

    assert [added.exit_code, listed.exit_code, removed.exit_code] == [0, 0, 0]
    assert subprocess_started is False


def test_doctor_reports_all_passes_without_runtime_side_effects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    discovered_configs: list[object] = []

    async def discover_models(config):
        discovered_configs.append(config)
        return [ModelInfo("model-a"), ModelInfo("model-b")]

    async def unexpected_runtime(cwd: Path) -> HostRuntime:
        raise AssertionError(f"doctor must not build an Agent runtime for {cwd}")

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._model_discovery", discover_models)
    monkeypatch.setattr("phi.cli.main._runtime_factory", unexpected_runtime)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Doctor" in result.stdout
    assert "Status" in result.stdout
    assert "Check" in result.stdout
    assert result.stdout.index("settings") < result.stdout.index("model-discovery")
    assert result.stdout.index("model-discovery") < result.stdout.index("default-model")
    assert result.stdout.count("PASS") == 3
    assert result.stderr == ""
    assert len(discovered_configs) == 1
    assert list(settings.session_dir.glob("*.metadata.json")) == []


def test_doctor_missing_credentials_skips_dependent_checks(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"api_key": SecretStr("")})
    discovery_called = False

    async def unexpected_discovery(config):
        del config
        nonlocal discovery_called
        discovery_called = True
        return []

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._model_discovery", unexpected_discovery)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert result.stdout.count("FAIL") == 1
    assert result.stdout.count("SKIP") == 2
    assert all(name in result.stdout for name in ("settings", "model-discovery", "default-model"))
    assert "PHI_API_KEY is required" in result.stderr
    assert discovery_called is False


def test_doctor_rejects_invalid_base_url_before_network(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"base_url": "not-a-url"})
    discovery_called = False

    async def unexpected_discovery(config):
        del config
        nonlocal discovery_called
        discovery_called = True
        return []

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._model_discovery", unexpected_discovery)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "settings" in result.stdout
    assert "PHI_BASE_URL must be an absolute HTTP(S) URL" in result.stderr
    assert discovery_called is False


def test_doctor_rejects_invalid_timeout_before_network(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"request_timeout_seconds": float("inf")})
    discovery_called = False

    async def unexpected_discovery(config):
        del config
        nonlocal discovery_called
        discovery_called = True
        return []

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._model_discovery", unexpected_discovery)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "settings" in result.stdout
    assert "PHI_REQUEST_TIMEOUT_SECONDS must be finite and positive" in result.stderr
    assert discovery_called is False


def test_doctor_redacts_known_credential_from_discovery_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret = "arbitrary-provider-secret-value"
    settings = _settings(tmp_path).model_copy(update={"api_key": SecretStr(secret)})

    async def failed_discovery(config):
        del config
        raise ModelHTTPError(401, f"upstream echoed {secret} without a label")

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._model_discovery", failed_discovery)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert result.stdout.count("PASS") == 1
    assert result.stdout.count("FAIL") == 1
    assert result.stdout.count("SKIP") == 1
    assert all(name in result.stdout for name in ("settings", "model-discovery", "default-model"))
    assert secret not in result.stderr
    assert "[REDACTED]" in result.stderr
    assert "Traceback" not in result.stderr


def test_doctor_reports_protocol_failure_and_missing_or_unavailable_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    async def malformed(config):
        del config
        raise ModelProtocolError("Model registry data must be a list")

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._model_discovery", malformed)
    malformed_result = runner.invoke(app, ["doctor"])

    async def available(config):
        del config
        return [ModelInfo("other")]

    monkeypatch.setattr("phi.cli.main._model_discovery", available)
    unavailable = runner.invoke(app, ["doctor"])
    monkeypatch.setattr(
        "phi.cli.main._settings_factory",
        lambda: settings.model_copy(update={"default_model": ""}),
    )
    missing = runner.invoke(app, ["doctor"])

    assert malformed_result.exit_code == 1
    assert "FAIL" in malformed_result.stdout
    assert "model-discovery" in malformed_result.stdout
    assert "Model registry data must be a list" in malformed_result.stderr
    assert unavailable.exit_code == 1
    assert "FAIL" in unavailable.stdout
    assert "default-model" in unavailable.stdout
    assert "Model 'model-a' is not available" in unavailable.stderr
    assert missing.exit_code == 1
    assert "FAIL" in missing.stdout
    assert "default-model" in missing.stdout
    assert "PHI_DEFAULT_MODEL is required" in missing.stderr


def test_doctor_redacts_credential_shaped_unavailable_default_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret_model = "sk-fake-secret-for-redaction"
    settings = _settings(tmp_path, default_model=secret_model)

    async def available(config):
        del config
        return [ModelInfo("other")]

    monkeypatch.setattr("phi.cli.main._settings_factory", lambda: settings)
    monkeypatch.setattr("phi.cli.main._model_discovery", available)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "default-model" in result.stdout
    assert secret_model not in result.stderr
    assert "Model '[REDACTED]' is not available" in result.stderr
