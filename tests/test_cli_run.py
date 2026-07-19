from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from phi.bootstrap import HostRuntime, build_runtime_resources
from phi.cli import app
from phi.instructions import PHI_BASE_INSTRUCTIONS
from phi.model import (
    ContentDelta,
    FinishEvent,
    Model,
    ModelHTTPError,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    ToolCall,
)
from phi.sessions import SessionStorage
from phi.settings import Settings
from phi.tools import HEADLESS_MODE, RuleBasedApprovalPolicy

runner = CliRunner()


@dataclass
class ScenarioRuntimeFactory:
    root: Path
    settings: Settings
    model: Model
    available_models: tuple[ModelInfo, ...]
    close_count: int = 0

    async def __call__(self, cwd: Path) -> HostRuntime:
        resources = await build_runtime_resources(
            cwd,
            base_instructions=PHI_BASE_INSTRUCTIONS,
            approval_policy=RuleBasedApprovalPolicy(HEADLESS_MODE),
            global_skill_root=self.root / "global-skills",
            global_agent_root=self.root / "global-agents",
            global_mcp_config_path=self.root / "global-mcp.json",
        )

        async def observe_close() -> None:
            self.close_count += 1

        return HostRuntime(
            settings=self.settings,
            model=self.model,
            available_models=self.available_models,
            storage=SessionStorage(self.settings.session_dir),
            resources=resources,
            close_callback=observe_close,
        )


def _settings(root: Path, *, default_model: str = "model-a") -> Settings:
    return Settings(
        api_key=SecretStr("test-key"),
        default_model=default_model,
        session_dir=root / "sessions",
    )


def _session_id(stderr: str) -> str:
    return next(
        line.removeprefix("session_id=")
        for line in stderr.splitlines()
        if line.startswith("session_id=")
    )


def test_json_mode_emits_the_same_redacted_ordered_records_as_trace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "secret-json-output-value"
    model = ScriptedModel(
        [
            [
                ContentDelta(f"api_key={secret}"),
                FinishEvent("stop", {}),
            ]
        ]
    )
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "respond", "--json"])

    assert result.exit_code == 0
    assert secret not in result.stdout
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["event_index"] for record in records] == list(range(len(records)))
    assert records[-1]["event_type"] == "run_finished"
    assert records[-1]["payload"]["status"] == "completed"
    assert records[-1]["payload"]["output"] == "api_key=[REDACTED]"
    assert {record["schema_version"] for record in records} == {1}
    assert factory.close_count == 1

    session_id = _session_id(result.stderr)
    trace_records = [
        json.loads(line)
        for line in SessionStorage(tmp_path / "sessions")
        .trace_path(session_id)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records == trace_records


def test_cancelled_json_run_emits_a_terminal_event_and_exits_130(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class CancelledModel:
        async def request(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError(f"ordinary Runs must stream: {request}")

        async def request_stream(self, request: ModelRequest):
            del request
            raise asyncio.CancelledError
            yield ContentDelta("unreachable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        CancelledModel(),
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "wait", "--json"])

    assert result.exit_code == 130
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records[-1]["event_type"] == "run_finished"
    assert records[-1]["payload"]["status"] == "cancelled"
    assert [record["event_index"] for record in records] == list(range(len(records)))
    assert factory.close_count == 1
    trace_records = [
        json.loads(line)
        for line in SessionStorage(tmp_path / "sessions")
        .trace_path(_session_id(result.stderr))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert trace_records == records


def test_run_resumes_the_current_leaf_with_prior_conversation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(content="first answer"),
            ModelResponse(content="second answer"),
        ]
    )
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    first = runner.invoke(app, ["run", "first question"])
    session_id = _session_id(first.stderr)
    second = runner.invoke(app, ["run", "follow up", "--session", session_id])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert _session_id(second.stderr) == session_id
    assert model.requests[1].messages[-3:] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "follow up"},
    ]
    assert factory.close_count == 2


def test_explicit_model_change_forks_a_branch_that_already_has_model_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(content="answer from a"),
            ModelResponse(content="answer from b"),
        ]
    )
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"), ModelInfo("model-b")),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    first = runner.invoke(app, ["run", "first"])
    parent_id = _session_id(first.stderr)
    storage = SessionStorage(tmp_path / "sessions")
    parent_leaf = asyncio.run(storage.load(parent_id)).metadata.leaf_id
    second = runner.invoke(
        app,
        ["run", "continue", "--session", parent_id, "--model", "model-b"],
    )

    fork_id = _session_id(second.stderr)
    fork = asyncio.run(storage.load(fork_id)).metadata
    assert second.exit_code == 0
    assert fork_id != parent_id
    assert fork.parent_session_id == parent_id
    assert fork.fork_point_entry_id == parent_leaf
    assert fork.model == "model-b"
    assert model.requests[1].model == "model-b"
    assert model.requests[1].messages[-3:] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer from a"},
        {"role": "user", "content": "continue"},
    ]


def test_unavailable_model_fails_before_creating_a_session_or_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="must not run")])
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "task", "--model", "missing"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Model 'missing' is not available" in result.stderr
    assert list((tmp_path / "sessions").glob("*.metadata.json")) == []
    assert model.requests == []
    assert factory.close_count == 1


def test_headless_policy_allows_reads_and_denies_mutating_and_unconfined_tools(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ground.txt").write_text("ground truth\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall("read-1", "read", {"path": "ground.txt"}),
                    ToolCall(
                        "write-1",
                        "write",
                        {"path": "changed.txt", "content": "must not exist"},
                    ),
                    ToolCall("bash-1", "bash", {"command": "touch escaped.txt"}),
                ]
            ),
            ModelResponse(content="recovered from denials"),
        ]
    )
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "inspect without changing anything"])

    assert result.exit_code == 0
    assert result.stdout == "recovered from denials\n"
    tool_messages = [message for message in model.requests[1].messages if message["role"] == "tool"]
    assert tool_messages == [
        {"role": "tool", "tool_call_id": "read-1", "content": "ground truth\n"},
        {
            "role": "tool",
            "tool_call_id": "write-1",
            "content": "approval_denied: write",
        },
        {
            "role": "tool",
            "tool_call_id": "bash-1",
            "content": "approval_denied: bash",
        },
    ]
    assert not (workspace / "changed.txt").exists()
    assert not (workspace / "escaped.txt").exists()


def test_failed_run_exits_1_without_leaking_provider_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "provider-secret-value"
    model = ScriptedModel(
        [ModelHTTPError(401, f'{{"api_key":"{secret}"}} Authorization: Bearer {secret}')]
    )
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "fail safely"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Run failed:" in result.stderr
    assert "[REDACTED]" in result.stderr
    assert secret not in result.stderr
    assert "Traceback" not in result.stderr


def test_max_step_run_exits_2_and_preserves_the_complete_tool_unit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("missing-1", "missing", {})])])
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "keep bounded", "--max-steps", "1"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Run exhausted its Step budget (1)" in result.stderr
    session_id = _session_id(result.stderr)
    state = asyncio.run(SessionStorage(tmp_path / "sessions").load_state(session_id))
    assert [entry.entry_type for entry in state.entries] == [
        "user_message",
        "assistant_message",
        "tool_result",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "   "],
        ["run", "task", "--max-steps", "0"],
        ["run", "task", "--max-steps", "not-an-integer"],
    ],
)
def test_usage_errors_are_rejected_before_runtime_construction(
    monkeypatch,
    arguments: list[str],
) -> None:
    factory_called = False

    async def unexpected_factory(cwd: Path) -> HostRuntime:
        nonlocal factory_called
        factory_called = True
        raise AssertionError(f"runtime must not be built for invalid input in {cwd}")

    monkeypatch.setattr("phi.cli.main._runtime_factory", unexpected_factory)

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert factory_called is False


def test_missing_api_key_fails_before_session_mutation_and_closes_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="must not run")])
    settings = Settings(
        api_key=SecretStr(""),
        default_model="model-a",
        session_dir=tmp_path / "sessions",
    )
    factory = ScenarioRuntimeFactory(
        tmp_path,
        settings,
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "task"])

    assert result.exit_code == 1
    assert "PHI_API_KEY is required" in result.stderr
    assert list((tmp_path / "sessions").glob("*.metadata.json")) == []
    assert model.requests == []
    assert factory.close_count == 1


def test_unknown_session_fails_before_model_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="must not run")])
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "task", "--session", "missing"])

    assert result.exit_code == 1
    assert "Session 'missing' was not found" in result.stderr
    assert result.stdout == ""
    assert model.requests == []
    assert list((tmp_path / "sessions").glob("*.metadata.json")) == []


def test_run_loads_project_runtime_inputs_and_reports_isolated_diagnostics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Project instruction ground truth.\n", encoding="utf-8")
    skill_path = workspace / ".phi" / "skills" / "inspect.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: inspect\ndescription: Inspect the project carefully.\n---\n"
        "Use project evidence.\n",
        encoding="utf-8",
    )
    agent_path = workspace / ".phi" / "agents" / "reviewer.md"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_text(
        "---\nname: reviewer\ndescription: Review project evidence.\n---\n"
        "Review the delegated task.\n",
        encoding="utf-8",
    )
    mcp_path = workspace / ".phi" / "mcp.json"
    mcp_path.write_text("not-json", encoding="utf-8")
    model = ScriptedModel([ModelResponse(content="runtime remained healthy")])
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)

    result = runner.invoke(app, ["run", "inspect"])

    assert result.exit_code == 0
    assert result.stdout == "runtime remained healthy\n"
    assert "warning:" in result.stderr
    assert str(mcp_path) in result.stderr
    system_message = model.requests[0].messages[0]
    assert system_message["role"] == "system"
    assert "Agent composed from a Model and a Harness" in system_message["content"]
    assert "Project instruction ground truth." in system_message["content"]
    assert "Inspect the project carefully." in system_message["content"]
    tool_names = {
        tool["function"]["name"]
        for tool in model.requests[0].tools
        if tool.get("type") == "function"
    }
    assert "skill_tool" in tool_names
    assert "spawn_agent" in tool_names


def test_incompatible_session_fails_before_a_second_model_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel([ModelResponse(content="first answer")])
    factory = ScenarioRuntimeFactory(
        tmp_path,
        _settings(tmp_path),
        model,
        (ModelInfo("model-a"),),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", factory)
    first = runner.invoke(app, ["run", "first"])
    session_id = _session_id(first.stderr)
    storage = SessionStorage(tmp_path / "sessions")
    journal_path = storage.journal_path(session_id)
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["schema_version"] = 2
    lines[0] = json.dumps(entry)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["run", "continue", "--session", session_id])

    assert result.exit_code == 1
    assert "unsupported schema version 2" in result.stderr
    assert result.stdout == ""
    assert len(model.requests) == 1
