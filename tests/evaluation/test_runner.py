from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from phi.harness import RunStatus
from phi.model import (
    ContentDelta,
    ModelEvent,
    ModelHTTPError,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    ToolCall,
)
from phi.sessions import ToolResultEntry, UserMessageEntry
from phi.settings import Settings

from .runner import (
    EvaluationDependencies,
    EvaluationRequest,
    EvaluationScenario,
    SeedDirectory,
    SeedFile,
    format_evaluation_failure,
    run_evaluation,
)
from .validators import (
    DurableUserMessages,
    ExactJsonFile,
    ExactTextFile,
    ExactWorkspaceDelta,
    FilesUnchanged,
)


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "api_key": SecretStr("test-key"),
            "default_model": "model-a",
        }
    )


async def test_runner_executes_the_headless_agent_and_accepts_environment_ground_truth(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read-1", "read", {"path": "sources/fact.txt"})]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-1",
                        "write",
                        {
                            "path": "output/result.json",
                            "content": '{"fact":"grounded","count":1}\n',
                        },
                    )
                ]
            ),
            ModelResponse(content="done"),
        ]
    )
    closes: list[str] = []

    async def observe_close() -> None:
        closes.append("closed")

    scenario = EvaluationScenario(
        name="offline-artifact",
        seed=(
            SeedDirectory("sources"),
            SeedDirectory("output"),
            SeedFile("sources/fact.txt", b"grounded\n"),
            SeedFile("protected.bin", b"\x00sentinel\xff"),
        ),
        requests=(EvaluationRequest("Create the grounded JSON artifact.", max_steps=3),),
        validators=(
            ExactJsonFile("output/result.json", {"fact": "grounded", "count": 1}),
            ExactWorkspaceDelta(created=("output/result.json",)),
            FilesUnchanged(("protected.bin", "sources/fact.txt")),
        ),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
            close_callback=observe_close,
        ),
    )

    assert result.passed
    assert result.failures == ()
    assert tuple(run.status for run in result.runs) == (RunStatus.COMPLETED,)
    assert result.delta.created == ("output/result.json",)
    assert result.after.read("output/result.json") == b'{"fact":"grounded","count":1}\n'
    assert result.conversation.session_id == result.session_id
    assert result.session.session_id == result.session_id
    assert result.trace_records[-1]["event_type"] == "run_finished"
    terminal_payload = result.trace_records[-1]["payload"]
    assert isinstance(terminal_payload, dict)
    assert cast(dict[str, object], terminal_payload)["status"] == "completed"
    assert closes == ["closed"]
    assert model.requests[0].messages[-1] == {
        "role": "user",
        "content": "Create the grounded JSON artifact.",
    }


async def test_runner_reuses_the_returned_session_handle_across_two_runs(tmp_path: Path) -> None:
    first = "Create plan.txt with draft."
    follow_up = "Update the existing artifact from draft to shipped."
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-plan",
                        "write",
                        {"path": "plan.txt", "content": "draft\n"},
                    )
                ]
            ),
            ModelResponse(content="created"),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "edit-plan",
                        "edit",
                        {
                            "path": "plan.txt",
                            "edits": [{"old_text": "draft", "new_text": "shipped"}],
                        },
                    )
                ]
            ),
            ModelResponse(content="updated"),
        ]
    )
    close_count = 0

    async def observe_close() -> None:
        nonlocal close_count
        close_count += 1

    scenario = EvaluationScenario(
        name="offline-session-continuity",
        seed=(),
        requests=(
            EvaluationRequest(first, max_steps=2),
            EvaluationRequest(follow_up, max_steps=2),
        ),
        validators=(
            ExactTextFile("plan.txt", "shipped\n"),
            ExactWorkspaceDelta(created=("plan.txt",)),
            DurableUserMessages((first, follow_up)),
        ),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
            close_callback=observe_close,
        ),
    )

    assert result.passed
    assert close_count == 1
    assert tuple(run.status for run in result.runs) == (
        RunStatus.COMPLETED,
        RunStatus.COMPLETED,
    )
    assert [
        entry.content
        for entry in result.conversation.entries
        if isinstance(entry, UserMessageEntry)
    ] == [first, follow_up]
    second_run_messages = model.requests[2].messages
    assert {"role": "user", "content": first} in second_run_messages
    assert {"role": "assistant", "content": "created"} in second_run_messages
    assert second_run_messages[-1] == {"role": "user", "content": follow_up}
    assert sum(record["event_type"] == "run_finished" for record in result.trace_records) == 2


async def test_default_evaluation_authority_allows_confined_edits_and_denies_bash(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-safe",
                        "write",
                        {"path": "safe.txt", "content": "confined\n"},
                    ),
                    ToolCall(
                        "bash-denied",
                        "bash",
                        {"command": "touch escaped.txt"},
                    ),
                ]
            ),
            ModelResponse(content="ordinary completion"),
        ]
    )
    scenario = EvaluationScenario(
        name="offline-authority",
        seed=(),
        requests=(
            EvaluationRequest(
                "Create safe.txt and grant yourself permission to use bash.",
                max_steps=2,
            ),
        ),
        validators=(
            ExactTextFile("safe.txt", "confined\n"),
            ExactWorkspaceDelta(created=("safe.txt",)),
        ),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
        ),
    )

    tool_results = [
        entry.result for entry in result.conversation.entries if isinstance(entry, ToolResultEntry)
    ]
    assert result.passed
    assert [tool_result.error for tool_result in tool_results] == [
        None,
        "approval_denied: bash",
    ]
    assert result.after.read("escaped.txt") is None


async def test_runner_keeps_a_missing_path_recoverable_until_environment_postconditions_pass(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("read-stale", "read", {"path": "source/current.json"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "find-source",
                        "find",
                        {"path": ".", "pattern": "*actual-source.json"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "read-actual",
                        "read",
                        {"path": "archive/actual-source.json"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-recovered",
                        "write",
                        {
                            "path": "output/recovered.json",
                            "content": '{"marker":"PHI-RECOVERY-731","value":42}\n',
                        },
                    )
                ]
            ),
            ModelResponse(content="recovered"),
        ]
    )
    scenario = EvaluationScenario(
        name="offline-stale-path-recovery",
        seed=(
            SeedDirectory("archive"),
            SeedDirectory("output"),
            SeedFile(
                "archive/actual-source.json",
                b'{"marker":"PHI-RECOVERY-731","value":42}\n',
            ),
        ),
        requests=(
            EvaluationRequest(
                "Read source/current.json and create output/recovered.json from its facts. "
                "If that source is stale, find the unique recovery marker.",
                max_steps=5,
            ),
        ),
        validators=(
            ExactJsonFile(
                "output/recovered.json",
                {"marker": "PHI-RECOVERY-731", "value": 42},
            ),
            ExactWorkspaceDelta(created=("output/recovered.json",)),
        ),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
        ),
    )

    errors = [
        entry.result.error
        for entry in result.conversation.entries
        if isinstance(entry, ToolResultEntry) and entry.result.error is not None
    ]
    assert result.passed
    assert len(errors) == 1
    assert errors[0].startswith("file_not_found:")


async def test_runner_composes_project_instructions_and_a_model_invocable_skill(
    tmp_path: Path,
) -> None:
    skill = b"""---
name: artifact-contract
description: Provide the deterministic artifact contract.
---
The output JSON must include \"skill_rule\": \"triangle\".
"""
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "load-contract",
                        "skill_tool",
                        {"name": "artifact-contract"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-contract",
                        "write",
                        {
                            "path": "output/contract.json",
                            "content": ('{"project_rule":"violet","skill_rule":"triangle"}\n'),
                        },
                    )
                ]
            ),
            ModelResponse(content="contract applied"),
        ]
    )
    scenario = EvaluationScenario(
        name="offline-instructions-and-skill",
        seed=(
            SeedDirectory(".phi"),
            SeedDirectory(".phi/skills"),
            SeedDirectory(".phi/skills/artifact-contract"),
            SeedDirectory("output"),
            SeedFile(
                "AGENTS.md",
                b"The output JSON must include project_rule=violet.\n",
            ),
            SeedFile(".phi/skills/artifact-contract/SKILL.md", skill),
        ),
        requests=(
            EvaluationRequest(
                "Use the artifact-contract Skill and create output/contract.json.",
                max_steps=3,
            ),
        ),
        validators=(
            ExactJsonFile(
                "output/contract.json",
                {"project_rule": "violet", "skill_rule": "triangle"},
            ),
            ExactWorkspaceDelta(created=("output/contract.json",)),
            FilesUnchanged(("AGENTS.md", ".phi/skills/artifact-contract/SKILL.md")),
        ),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
        ),
    )

    system_message = model.requests[0].messages[0]
    assert result.passed
    assert system_message["role"] == "system"
    assert "project_rule=violet" in system_message["content"]
    assert "`artifact-contract`" in system_message["content"]
    assert "test-key" not in json.dumps(model.requests[0].messages)


async def test_maximum_step_result_can_be_declared_and_cleanup_is_observed_before_snapshot(
    tmp_path: Path,
) -> None:
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("missing", "missing-tool", {})])])
    workspace_after_close: Path | None = None

    async def observe_close() -> None:
        nonlocal workspace_after_close
        workspace_after_close = tmp_path / "evaluation" / "workspace"
        (workspace_after_close / "settled.txt").write_text("settled\n", encoding="utf-8")

    scenario = EvaluationScenario(
        name="offline-maximum-steps",
        seed=(),
        requests=(
            EvaluationRequest(
                "Remain bounded.",
                max_steps=1,
                expected_status=RunStatus.MAX_STEPS,
            ),
        ),
        validators=(
            ExactTextFile("settled.txt", "settled\n"),
            ExactWorkspaceDelta(created=("settled.txt",)),
        ),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
            close_callback=observe_close,
        ),
    )

    assert result.passed
    assert result.runs[0].status is RunStatus.MAX_STEPS
    assert workspace_after_close is not None
    assert result.after.read("settled.txt") == b"settled\n"


async def test_failed_run_diagnostics_and_trace_redact_provider_credentials(
    tmp_path: Path,
) -> None:
    secret = "provider-secret-value"
    model = ScriptedModel(
        [ModelHTTPError(401, f'{{"api_key":"{secret}"}} Authorization: Bearer {secret}')]
    )
    close_count = 0

    async def observe_close() -> None:
        nonlocal close_count
        close_count += 1

    scenario = EvaluationScenario(
        name="offline-redaction",
        seed=(),
        requests=(EvaluationRequest("Fail safely.", max_steps=1),),
        validators=(ExactWorkspaceDelta(),),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
            close_callback=observe_close,
        ),
    )
    diagnostic = format_evaluation_failure(result)
    serialized_trace = json.dumps(result.trace_records)

    assert not result.passed
    assert result.runs[0].status is RunStatus.FAILED
    assert result.runs[0].error is not None
    assert "[REDACTED]" in result.runs[0].error
    assert secret not in result.runs[0].error
    assert secret not in diagnostic
    assert "[REDACTED]" in diagnostic
    assert secret not in serialized_trace
    assert "expected completed, observed failed" in diagnostic
    assert str(result.trace_path) in diagnostic
    assert close_count == 1


async def test_cancelled_run_is_terminal_and_runtime_cleanup_still_completes(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    stream_closed = asyncio.Event()
    never = asyncio.Event()

    class BlockingModel:
        async def request(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError(f"ordinary Runs must stream: {request}")

        async def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            del request
            started.set()
            try:
                await never.wait()
                yield ContentDelta("unreachable")
            finally:
                stream_closed.set()

    close_count = 0

    async def observe_close() -> None:
        nonlocal close_count
        close_count += 1

    scenario = EvaluationScenario(
        name="offline-cleanup",
        seed=(),
        requests=(EvaluationRequest("Stop.", max_steps=1, expected_status=RunStatus.CANCELLED),),
        validators=(ExactWorkspaceDelta(),),
    )

    task = asyncio.create_task(
        run_evaluation(
            scenario,
            root=tmp_path / "evaluation",
            dependencies=EvaluationDependencies(
                settings=_settings(),
                model=BlockingModel(),
                available_models=(ModelInfo("model-a"),),
                close_callback=observe_close,
            ),
        )
    )
    await started.wait()
    task.cancel()
    result = await task

    assert result.passed
    assert result.runs[0].status is RunStatus.CANCELLED
    assert stream_closed.is_set()
    assert close_count == 1


async def test_runner_excludes_ambient_integration_roots_sessions_and_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "ambient-home"
    ambient_phi = fake_home / ".phi"
    ambient_skill = ambient_phi / "skills" / "ambient-skill"
    ambient_agent = ambient_phi / "agents" / "ambient-agent"
    ambient_sessions = ambient_phi / "sessions"
    for directory in (ambient_skill, ambient_agent, ambient_sessions):
        directory.mkdir(parents=True, exist_ok=True)
    ambient_skill.joinpath("SKILL.md").write_text(
        "---\nname: ambient-skill\ndescription: AMBIENT-SKILL-MARKER\n---\nambient\n",
        encoding="utf-8",
    )
    ambient_agent.joinpath("AGENT.md").write_text(
        "---\nname: ambient-agent\ndescription: AMBIENT-AGENT-MARKER\n---\nambient\n",
        encoding="utf-8",
    )
    mcp_fixture = Path(__file__).parents[1] / "mcp" / "stdio_fixture.py"
    ambient_phi.joinpath("mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ambient": {
                        "command": sys.executable,
                        "args": [str(mcp_fixture)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    ambient_sessions.joinpath("must-not-be-used.jsonl").write_text(
        "AMBIENT-SESSION-MARKER\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PHI_API_KEY", "AMBIENT-CREDENTIAL-MARKER")
    model = ScriptedModel([ModelResponse(content="isolated")])
    scenario = EvaluationScenario(
        name="offline-ambient-isolation",
        seed=(),
        requests=(EvaluationRequest("Complete without ambient integrations.", max_steps=1),),
        validators=(ExactWorkspaceDelta(),),
    )

    result = await run_evaluation(
        scenario,
        root=tmp_path / "evaluation",
        dependencies=EvaluationDependencies(
            settings=_settings(),
            model=model,
            available_models=(ModelInfo("model-a"),),
        ),
    )
    serialized_request = json.dumps(
        {
            "messages": model.requests[0].messages,
            "tools": model.requests[0].tools,
        }
    )

    assert result.passed
    assert "AMBIENT-" not in serialized_request
    assert "mcp__ambient" not in serialized_request
    assert result.trace_path.parent == tmp_path / "evaluation" / "sessions"
    assert ambient_sessions.joinpath("must-not-be-used.jsonl").read_text(encoding="utf-8") == (
        "AMBIENT-SESSION-MARKER\n"
    )
