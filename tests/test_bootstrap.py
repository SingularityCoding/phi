from pathlib import Path

import pytest

from phi.bootstrap import CwdRuntimeBootstrap, build_runtime_resources
from phi.model import ModelResponse, ScriptedModel
from phi.sessions import (
    SessionStorage,
    create_session,
    inspect_context,
    manual_compact,
    send_message,
)
from phi.settings import Settings
from phi.skills import SkillNotFoundError
from phi.tools import DEFAULT_MODE, RuleBasedApprovalPolicy


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


def test_cwd_assembly_orders_stable_sections_and_exposes_only_model_skills(
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

    resources = build_runtime_resources(
        cwd,
        base_instructions="Phi base.\n",
        personal_instructions="Personal rules.\n",
        global_skill_root=global_root,
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


def test_trusted_user_invocation_can_select_a_model_disabled_skill(tmp_path: Path) -> None:
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
    resources = build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    assert resources.invoke_skill("user-only") == "Trusted body.\n"
    assert resources.stable_instructions == (
        "--- BEGIN PHI BASE INSTRUCTIONS ---\nPhi base.\n--- END PHI BASE INSTRUCTIONS ---"
    )
    assert all(spec["function"]["name"] != "skill_tool" for spec in resources.tools.specs())
    with pytest.raises(SkillNotFoundError, match="missing"):
        resources.invoke_skill("missing")


def test_runtime_resources_are_reused_until_cwd_changes_or_rebuild_is_requested(
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
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    first = bootstrap.load(first_cwd)
    (first_cwd / "AGENTS.md").write_text("Rebuilt version.\n", encoding="utf-8")
    reused = bootstrap.load(first_cwd)
    rebuilt = bootstrap.load(first_cwd, rebuild=True)
    changed = bootstrap.load(second_cwd)

    assert reused is first
    assert "First version." in reused.stable_instructions
    assert rebuilt is not first
    assert "Rebuilt version." in rebuilt.stable_instructions
    assert changed.cwd == second_cwd.resolve()
    assert "Second cwd." in changed.stable_instructions


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
    resources = build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
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


def test_project_skill_discovery_honors_repository_root_ignore_rules(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / ".gitignore").write_text(".phi/skills/ignored.md\n", encoding="utf-8")
    _write_skill(
        cwd / ".phi" / "skills" / "ignored.md",
        name="ignored",
        description="Ignored Skill.",
        body="Ignored body.\n",
    )

    resources = build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    assert resources.skill_discovery.skills == {}
    assert "MODEL-INVOKABLE SKILLS" not in resources.stable_instructions
    assert resources.tools.get("skill_tool") is None
