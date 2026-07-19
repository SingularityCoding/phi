import asyncio
from pathlib import Path

from pydantic import SecretStr
from typer.testing import CliRunner

from phi.bootstrap import HostRuntime, build_runtime_resources
from phi.cli import app
from phi.model import ModelInfo, ModelResponse, ScriptedModel
from phi.sessions import SessionStorage, materialize_conversation, resume_session
from phi.settings import Settings
from phi.tools import BYPASS_MODE, RuleBasedApprovalPolicy

runner = CliRunner()


def test_bare_invocation_launches_tui(monkeypatch):
    # patch where it's looked up (phi.cli.main.run_tui), not where it's
    # defined (phi.ui.run) — `from x import y` binds a local name at import time.
    calls = []
    monkeypatch.setattr("phi.cli.main.run_tui", lambda: calls.append(True))

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert calls == [True]


def test_run_creates_a_durable_session_and_keeps_stdout_machine_usable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = tmp_path / "sessions"
    model = ScriptedModel([ModelResponse(content="completed output")])
    settings = Settings(
        api_key=SecretStr("test-key"),
        default_model="model-a",
        session_dir=session_root,
    )

    async def runtime_factory(cwd: Path) -> HostRuntime:
        resources = await build_runtime_resources(
            cwd,
            base_instructions="Phi test base.",
            approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
            global_skill_root=tmp_path / "global-skills",
            global_agent_root=tmp_path / "global-agents",
            global_mcp_config_path=tmp_path / "global-mcp.json",
        )
        return HostRuntime(
            settings=settings,
            model=model,
            available_models=(ModelInfo("model-a"),),
            storage=SessionStorage(session_root),
            resources=resources,
        )

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", runtime_factory)

    result = runner.invoke(app, ["run", "say hello"])

    assert result.exit_code == 0
    assert result.stdout == "completed output\n"
    assert result.stderr.startswith("session_id=")
    session_id = result.stderr.splitlines()[0].removeprefix("session_id=")

    async def load_conversation() -> tuple[str, list[str]]:
        storage = SessionStorage(session_root)
        view = await materialize_conversation(storage, await resume_session(storage, session_id))
        return view.model or "", [entry.entry_type for entry in view.entries]

    selected_model, entry_types = asyncio.run(load_conversation())
    assert selected_model == "model-a"
    assert entry_types == ["user_message", "assistant_message"]
    assert model.requests[0].messages[-1] == {"role": "user", "content": "say hello"}


def test_run_rejects_a_blank_explicit_model_before_session_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = tmp_path / "sessions"
    model = ScriptedModel([ModelResponse(content="must not run")])
    settings = Settings(
        api_key=SecretStr("test-key"),
        default_model="model-a",
        session_dir=session_root,
    )

    async def runtime_factory(cwd: Path) -> HostRuntime:
        resources = await build_runtime_resources(
            cwd,
            base_instructions="Phi test base.",
            approval_policy=RuleBasedApprovalPolicy(BYPASS_MODE),
            global_skill_root=tmp_path / "global-skills",
            global_agent_root=tmp_path / "global-agents",
            global_mcp_config_path=tmp_path / "global-mcp.json",
        )
        return HostRuntime(
            settings=settings,
            model=model,
            available_models=(ModelInfo("model-a"),),
            storage=SessionStorage(session_root),
            resources=resources,
        )

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("phi.cli.main._runtime_factory", runtime_factory)

    result = runner.invoke(app, ["run", "task", "--model", "   "])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "--model must contain non-whitespace text" in result.stderr
    assert list(session_root.glob("*.metadata.json")) == []
    assert model.requests == []
