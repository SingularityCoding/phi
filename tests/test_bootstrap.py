import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from phi.bootstrap import CwdRuntimeBootstrap, build_headless_runtime, build_runtime_resources
from phi.mcp import McpConfigDiagnostic
from phi.model import ModelConfig, ModelInfo, ModelResponse, ScriptedModel
from phi.sessions import (
    SessionStorage,
    create_session,
    inspect_context,
    manual_compact,
    send_message,
)
from phi.settings import Settings
from phi.skills import SkillNotFoundError
from phi.tools import DEFAULT_MODE, ApprovalClass, RuleBasedApprovalPolicy


def _write_skill(
    source: Path,
    *,
    name: str,
    description: str,
    body: str,
    disabled: bool = False,
) -> None:
    source.parent.mkdir(parents=True, exist_ok=True)
    disabled_line = "disable-model-invocation: true\n" if disabled else ""
    source.write_text(
        f"---\nname: {name}\ndescription: {description}\n{disabled_line}---\n{body}",
        encoding="utf-8",
    )


def _write_agent_definition(
    source: Path,
    *,
    name: str,
    description: str,
    body: str,
    disabled: bool = False,
    extra_frontmatter: str = "",
) -> None:
    source.parent.mkdir(parents=True, exist_ok=True)
    disabled_line = "disable-model-invocation: true\n" if disabled else ""
    source.write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"{disabled_line}{extra_frontmatter}---\n{body}",
        encoding="utf-8",
    )


async def test_cwd_agent_definitions_drive_the_model_visible_delegation_tools(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    global_root = tmp_path / "global-agents"
    project_root = cwd / ".phi" / "agents"
    _write_agent_definition(
        global_root / "shared.md",
        name="shared",
        description="Use the shared specialist.",
        body="Global specialist instructions.\n",
    )
    _write_agent_definition(
        project_root / "alpha.md",
        name="alpha",
        description="Use the alpha specialist.",
        body="Project specialist instructions.\n",
    )
    _write_agent_definition(
        project_root / "private.md",
        name="private",
        description="Trusted callers only.",
        body="Private specialist instructions.\n",
        disabled=True,
    )
    _write_agent_definition(
        project_root / "shared.md",
        name="shared",
        description="Malformed project override.",
        body="This invalid override must not erase the global definition.\n",
        extra_frontmatter="unexpected-field: true\n",
    )

    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=global_root,
        project_agent_root=project_root,
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    assert tuple(resources.agent_definitions.definitions) == ("shared", "alpha", "private")
    assert resources.agent_definitions.definitions["shared"].system_prompt == (
        "Global specialist instructions.\n"
    )
    assert any(
        diagnostic.source_path == project_root / "shared.md"
        and "unknown field" in diagnostic.reason
        for diagnostic in resources.agent_definitions.diagnostics
    )
    assert [tool.name for tool in resources.agent_tools] == [
        "spawn_agent",
        "check_agent",
        "steer_agent",
        "list_agents",
        "close_agent",
    ]
    spawn_tool = resources.tools.get("spawn_agent")
    assert spawn_tool is not None
    assert spawn_tool.approval_class is ApprovalClass.UNCONFINED
    for name in ("check_agent", "steer_agent", "list_agents", "close_agent"):
        management_tool = resources.tools.get(name)
        assert management_tool is not None
        assert management_tool.approval_class is ApprovalClass.READ_ONLY
    spawn_spec = next(
        spec for spec in resources.tools.specs() if spec["function"]["name"] == "spawn_agent"
    )
    assert "- `alpha`: Use the alpha specialist." in spawn_spec["function"]["description"]
    assert "- `shared`: Use the shared specialist." in spawn_spec["function"]["description"]
    assert "private" not in spawn_spec["function"]["description"]
    assert resources.tools.get("wait_agent") is None
    await resources.close()


async def test_project_agent_discovery_honors_package_ignore_and_symlink_boundaries(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    project_root = cwd / ".phi" / "agents"
    (cwd / ".gitignore").write_text(
        ".phi/agents/ignored.md\n",
        encoding="utf-8",
    )
    _write_agent_definition(
        project_root / "ignored.md",
        name="ignored",
        description="Ignored definition.",
        body="Ignored.\n",
    )
    _write_agent_definition(
        project_root / "package-name" / "AGENT.md",
        name="package-name",
        description="Packaged definition.",
        body="Packaged.\n",
    )
    _write_agent_definition(
        project_root / "a" / "duplicate.md",
        name="duplicate",
        description="First valid definition.",
        body="First.\n",
    )
    _write_agent_definition(
        project_root / "b" / "duplicate.md",
        name="duplicate",
        description="Later colliding definition.",
        body="Second.\n",
    )
    _write_agent_definition(
        project_root / "node_modules" / "pruned.md",
        name="pruned",
        description="Pruned definition.",
        body="Pruned.\n",
    )
    external = tmp_path / "linked.md"
    _write_agent_definition(
        external,
        name="linked",
        description="Linked definition.",
        body="Linked.\n",
    )
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "linked.md").symlink_to(external)

    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global-skills",
        global_agent_root=tmp_path / "global-agents",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    assert tuple(resources.agent_definitions.definitions) == ("duplicate", "package-name")
    assert resources.agent_definitions.definitions["duplicate"].system_prompt == "First.\n"
    assert any(
        diagnostic.source_path == project_root / "b" / "duplicate.md"
        and "collision" in diagnostic.reason
        for diagnostic in resources.agent_definitions.diagnostics
    )
    await resources.close()


def _write_mcp_config(cwd: Path, pid_path: Path) -> None:
    source = cwd / ".phi" / "mcp.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / "mcp" / "stdio_fixture.py"
    source.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": {
                        "command": sys.executable,
                        "args": [str(fixture)],
                        "env": {"PHI_MCP_PID_FILE": str(pid_path)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


async def test_cwd_assembly_orders_stable_sections_and_exposes_only_model_skills(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("Project rules.\n", encoding="utf-8")
    global_root = tmp_path / "global"
    project_root = cwd / ".phi" / "skills"
    _write_skill(
        global_root / "zeta.md",
        name="zeta",
        description="Zeta workflow.",
        body="Zeta body.\n",
    )
    _write_skill(
        project_root / "alpha.md",
        name="alpha",
        description="Alpha workflow.",
        body="Alpha body.\n",
    )
    _write_skill(
        project_root / "user-only.md",
        name="user-only",
        description="Private user workflow.",
        body="Never disclose this body.\n",
        disabled=True,
    )

    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.\n",
        personal_instructions="Personal rules.\n",
        global_skill_root=global_root,
        global_mcp_config_path=tmp_path / "global-mcp.json",
        project_skill_root=project_root,
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    assert resources.stable_instructions == (
        "--- BEGIN PHI BASE INSTRUCTIONS ---\n"
        "Phi base.\n"
        "--- END PHI BASE INSTRUCTIONS ---\n\n"
        "--- BEGIN PERSONAL INSTRUCTIONS ---\n"
        "Personal rules.\n"
        "--- END PERSONAL INSTRUCTIONS ---\n\n"
        "--- BEGIN PROJECT INSTRUCTIONS ---\n"
        "Project rules.\n"
        "--- END PROJECT INSTRUCTIONS ---\n\n"
        "--- BEGIN MODEL-INVOKABLE SKILLS ---\n"
        "Load a Skill by calling `skill_tool` with its exact name.\n"
        "- `alpha`: Alpha workflow.\n"
        "- `zeta`: Zeta workflow.\n"
        "--- END MODEL-INVOKABLE SKILLS ---"
    )
    assert tuple(resources.skill_discovery.skills) == ("zeta", "alpha", "user-only")
    skill_spec = next(
        spec for spec in resources.tools.specs() if spec["function"]["name"] == "skill_tool"
    )
    assert skill_spec == {
        "type": "function",
        "function": {
            "name": "skill_tool",
            "description": "Load one Model-invocable Agent Skill by exact name.",
            "parameters": {
                "additionalProperties": False,
                "properties": {"name": {"title": "Name", "type": "string"}},
                "required": ["name"],
                "title": "LoadSkillArguments",
                "type": "object",
            },
        },
    }


async def test_trusted_user_invocation_can_select_a_model_disabled_skill(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    project_root = cwd / ".phi" / "skills"
    _write_skill(
        project_root / "user-only.md",
        name="user-only",
        description="User-selected workflow.",
        body="Trusted body.\n",
        disabled=True,
    )
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    assert resources.invoke_skill("user-only") == "Trusted body.\n"
    assert resources.stable_instructions == (
        "--- BEGIN PHI BASE INSTRUCTIONS ---\nPhi base.\n--- END PHI BASE INSTRUCTIONS ---"
    )
    assert all(spec["function"]["name"] != "skill_tool" for spec in resources.tools.specs())
    with pytest.raises(SkillNotFoundError, match="missing"):
        resources.invoke_skill("missing")


async def test_runtime_resources_are_reused_until_cwd_changes_or_rebuild_is_requested(
    tmp_path: Path,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    (first_cwd / "AGENTS.md").write_text("First version.\n", encoding="utf-8")
    (second_cwd / "AGENTS.md").write_text("Second cwd.\n", encoding="utf-8")
    bootstrap = CwdRuntimeBootstrap(
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    first = await bootstrap.load(first_cwd)
    (first_cwd / "AGENTS.md").write_text("Rebuilt version.\n", encoding="utf-8")
    reused = await bootstrap.load(first_cwd)
    rebuilt = await bootstrap.load(first_cwd, rebuild=True)
    changed = await bootstrap.load(second_cwd)

    assert reused is first
    assert "First version." in reused.stable_instructions
    assert rebuilt is not first
    assert "Rebuilt version." in rebuilt.stable_instructions
    assert changed.cwd == second_cwd.resolve()
    assert "Second cwd." in changed.stable_instructions
    await bootstrap.close()


async def test_compaction_rebuild_keeps_the_exact_assembled_stable_context(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("Project rules.\n", encoding="utf-8")
    _write_skill(
        cwd / ".phi" / "skills" / "explain.md",
        name="explain",
        description="Explain concepts.",
        body="Explain with examples.\n",
    )
    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )
    storage = SessionStorage(tmp_path / "sessions")
    handle = await create_session(storage, model="model-a")
    for prompt, answer in (("one", "answer one"), ("two", "answer two")):
        handle, _ = await send_message(
            handle,
            prompt,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=resources.tools,
            dispatcher=resources.dispatcher,
            stable_instructions=resources.stable_instructions,
            max_steps=1,
        )
    before = await inspect_context(
        storage,
        handle,
        settings=Settings(),
        model_info=None,
        tools=resources.tools,
        stable_instructions=resources.stable_instructions,
    )

    compacted = await manual_compact(
        handle,
        storage=storage,
        settings=Settings(compaction_keep_recent_tokens=0),
        model=ScriptedModel([ModelResponse(content="Earlier summary.")]),
        model_info=None,
        tools=resources.tools,
        stable_instructions=resources.stable_instructions,
    )
    after = await inspect_context(
        storage,
        compacted,
        settings=Settings(),
        model_info=None,
        tools=resources.tools,
        stable_instructions=resources.stable_instructions,
    )

    assert before.context.system_prompt == resources.stable_instructions
    assert after.context.system_prompt == before.context.system_prompt
    assert after.context.tools == before.context.tools
    assert after.context.dropped_summary == "Earlier summary."


async def test_project_skill_discovery_honors_repository_root_ignore_rules(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / ".gitignore").write_text(".phi/skills/ignored.md\n", encoding="utf-8")
    _write_skill(
        cwd / ".phi" / "skills" / "ignored.md",
        name="ignored",
        description="Ignored Skill.",
        body="Ignored body.\n",
    )

    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    assert resources.skill_discovery.skills == {}
    assert "MODEL-INVOKABLE SKILLS" not in resources.stable_instructions
    assert resources.tools.get("skill_tool") is None


async def test_bootstrap_rebuild_cwd_switch_and_shutdown_replace_mcp_lifetimes(
    tmp_path: Path,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    first_pid_path = tmp_path / "first.pid"
    second_pid_path = tmp_path / "second.pid"
    _write_mcp_config(first_cwd, first_pid_path)
    _write_mcp_config(second_cwd, second_pid_path)
    bootstrap = CwdRuntimeBootstrap(
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "skills",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    first = await bootstrap.load(first_cwd)
    first_pid = int(first_pid_path.read_text(encoding="utf-8"))
    assert first.mcp.server_ids == ("fixture",)
    assert await bootstrap.load(first_cwd) is first

    rebuilt = await bootstrap.load(first_cwd, rebuild=True)
    rebuilt_pid = int(first_pid_path.read_text(encoding="utf-8"))
    assert rebuilt is not first
    with pytest.raises(ProcessLookupError):
        os.kill(first_pid, 0)

    changed = await bootstrap.load(second_cwd)
    changed_pid = int(second_pid_path.read_text(encoding="utf-8"))
    assert changed.cwd == second_cwd.resolve()
    with pytest.raises(ProcessLookupError):
        os.kill(rebuilt_pid, 0)

    await bootstrap.close()
    await bootstrap.close()
    with pytest.raises(ProcessLookupError):
        os.kill(changed_pid, 0)


async def test_bootstrap_closes_started_mcp_when_agent_tool_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    class FakeMcpRuntime:
        diagnostics: tuple[()] = ()

        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    fake_mcp = FakeMcpRuntime()

    async def connect_fake_mcp(*args: object, **kwargs: object) -> FakeMcpRuntime:
        del args, kwargs
        return fake_mcp

    def fail_agent_tool_construction(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("agent tool construction failed")

    monkeypatch.setattr("phi.bootstrap.connect_mcp_servers", connect_fake_mcp)
    monkeypatch.setattr("phi.bootstrap.build_agent_tools", fail_agent_tool_construction)

    with pytest.raises(RuntimeError, match="agent tool construction failed"):
        await build_runtime_resources(
            cwd,
            base_instructions="Phi base.",
            global_skill_root=tmp_path / "global-skills",
            global_agent_root=tmp_path / "global-agents",
            global_mcp_config_path=tmp_path / "global-mcp.json",
            approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
        )

    assert fake_mcp.closed


async def test_invalid_mcp_config_fails_closed_without_disabling_non_mcp_runtime(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    config_path = cwd / ".phi" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    secret = "diagnostics-must-not-leak-this-value"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "invalid": {
                        "command": "server",
                        "env": {"TOKEN": [secret]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    resources = await build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "skills",
        global_mcp_config_path=tmp_path / "global-mcp.json",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )
    try:
        assert resources.mcp.server_ids == ()
        assert resources.tools.get("read") is not None
        assert len(resources.diagnostics) == 1
        assert isinstance(resources.diagnostics[0], McpConfigDiagnostic)
        assert resources.diagnostics[0].source_path == config_path
        assert secret not in repr(resources.diagnostics)
    finally:
        await resources.close()


async def test_host_runtime_composes_one_catalog_client_and_closes_it_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    settings = Settings(
        api_key=SecretStr("test-key"),
        default_model="model-a",
        session_dir=tmp_path / "sessions",
    )

    class ObservableClient:
        def __init__(self) -> None:
            self.close_count = 0

        async def aclose(self) -> None:
            self.close_count += 1

    client = ObservableClient()
    catalogs: list[tuple[ModelConfig, object]] = []

    async def static_catalog(
        config: ModelConfig,
        *,
        client: object,
    ) -> list[ModelInfo]:
        catalogs.append((config, client))
        return [ModelInfo("model-a", max_input_tokens=8_000)]

    monkeypatch.setattr("phi.bootstrap.Settings", lambda: settings)
    monkeypatch.setattr("phi.bootstrap.httpx.AsyncClient", lambda: client)
    monkeypatch.setattr("phi.bootstrap.list_available_models", static_catalog)

    runtime = await build_headless_runtime(cwd)

    assert len(catalogs) == 1
    config, catalog_client = catalogs[0]
    assert config.default_model == "model-a"
    assert config.api_key.get_secret_value() == "test-key"
    assert catalog_client is client
    assert runtime.available_models == (ModelInfo("model-a", max_input_tokens=8_000),)
    assert runtime.storage.root == tmp_path / "sessions"
    assert "Agent composed from a Model and a Harness" in runtime.resources.stable_instructions

    await runtime.close()
    await runtime.close()

    assert client.close_count == 1


async def test_host_runtime_closes_its_client_when_model_catalog_loading_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_key=SecretStr("test-key"),
        default_model="model-a",
        session_dir=tmp_path / "sessions",
    )

    class ObservableClient:
        def __init__(self) -> None:
            self.close_count = 0

        async def aclose(self) -> None:
            self.close_count += 1

    client = ObservableClient()

    async def failed_catalog(*args: object, **kwargs: object) -> list[ModelInfo]:
        del args, kwargs
        raise RuntimeError("catalog failed")

    monkeypatch.setattr("phi.bootstrap.Settings", lambda: settings)
    monkeypatch.setattr("phi.bootstrap.httpx.AsyncClient", lambda: client)
    monkeypatch.setattr("phi.bootstrap.list_available_models", failed_catalog)

    with pytest.raises(RuntimeError, match="catalog failed"):
        await build_headless_runtime(tmp_path)

    assert client.close_count == 1
