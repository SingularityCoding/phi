import json
from pathlib import Path

from phi.bootstrap import build_runtime_resources
from phi.harness import EventBus, RunEvent, RunStatus, ToolCallCompleted, ToolCallStarted
from phi.model import ModelResponse, ScriptedModel, ToolCall
from phi.sessions import SessionStorage, create_session, send_message
from phi.settings import Settings
from phi.tools import DEFAULT_MODE, ApprovalClass, RuleBasedApprovalPolicy


def _write_skill(source: Path, *, disabled: bool = False) -> None:
    source.parent.mkdir(parents=True, exist_ok=True)
    disabled_line = "disable-model-invocation: true\n" if disabled else ""
    source.write_text(
        "---\n"
        f"name: {source.stem}\n"
        f"description: Load {source.stem}.\n"
        f"{disabled_line}"
        "---\n"
        f"Instructions for {source.stem}.\n",
        encoding="utf-8",
    )


async def test_preloaded_skill_activation_uses_the_existing_run_events_and_trace(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    source = cwd / ".phi" / "skills" / "explain.md"
    _write_skill(source)
    resources = build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )
    source.unlink()
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("skill-call", "skill_tool", {"name": "explain"})]),
            ModelResponse(content="Applied the Skill."),
        ]
    )
    storage = SessionStorage(tmp_path / "sessions")
    handle = await create_session(storage, model="model-a")
    events: list[RunEvent] = []

    updated, result = await send_message(
        handle,
        "Explain this.",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=resources.tools,
        dispatcher=resources.dispatcher,
        stable_instructions=resources.stable_instructions,
        max_steps=2,
        events=EventBus([events.append]),
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Applied the Skill."
    assert result.steps[0].tool_results[0].output == "Instructions for explain.\n"
    assert resources.invoke_skill("explain") == result.steps[0].tool_results[0].output
    assert [
        type(event) for event in events if isinstance(event, (ToolCallStarted, ToolCallCompleted))
    ] == [
        ToolCallStarted,
        ToolCallCompleted,
    ]
    assert model.requests[0].messages[0] == {
        "role": "system",
        "content": resources.stable_instructions,
    }
    trace_records = [
        json.loads(line)
        for line in storage.trace_path(updated.session_id).read_text(encoding="utf-8").splitlines()
    ]
    completed = next(
        record for record in trace_records if record["event_type"] == "tool_call_completed"
    )
    assert completed["payload"]["result"] == {
        "call_id": "skill-call",
        "error": None,
        "output": "Instructions for explain.\n",
    }


async def test_skill_tool_returns_nondisclosing_errors_for_unknown_and_disabled_names(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    enabled = cwd / ".phi" / "skills" / "enabled.md"
    disabled = cwd / ".phi" / "skills" / "disabled.md"
    _write_skill(enabled)
    _write_skill(disabled, disabled=True)
    resources = build_runtime_resources(
        cwd,
        base_instructions="Phi base.",
        global_skill_root=tmp_path / "global",
        approval_policy=RuleBasedApprovalPolicy(DEFAULT_MODE),
    )

    unknown = await resources.dispatcher.dispatch(
        ToolCall("unknown", "skill_tool", {"name": "missing"})
    )
    rejected = await resources.dispatcher.dispatch(
        ToolCall("disabled", "skill_tool", {"name": "disabled"})
    )

    expected_error = "skill_unavailable: no Model-invocable Skill has that exact name"
    assert unknown.output == "" and unknown.error == expected_error
    assert rejected.output == "" and rejected.error == expected_error
    assert "Instructions for disabled" not in rejected.error
    assert str(disabled) not in rejected.error
    skill_tool = resources.tools.get("skill_tool")
    assert skill_tool is not None
    assert skill_tool.approval_class is ApprovalClass.READ_ONLY
