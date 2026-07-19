from __future__ import annotations

from pathlib import Path

import pytest

from phi.harness import RunStatus
from phi.model import ToolCall, ToolResult
from phi.sessions import (
    AssistantMessageEntry,
    ConversationView,
    ToolResultEntry,
    UserMessageEntry,
)

from .support import compare_workspace_snapshots, snapshot_workspace
from .validators import (
    DurableRequestSegments,
    DurableSingleBranch,
    DurableToolOutcome,
    DurableUserMessages,
    EvaluationObservation,
    ExactJsonFile,
    ExactTextFile,
    ExactWorkspaceDelta,
    FilesUnchanged,
    evaluate_validators,
)


def _observation(
    workspace: Path,
    *,
    before,
    entries=(),
) -> EvaluationObservation:
    after = snapshot_workspace(workspace)
    return EvaluationObservation(
        before=before,
        after=after,
        delta=compare_workspace_snapshots(before, after),
        run_statuses=(RunStatus.COMPLETED,),
        conversation=ConversationView(
            session_id="session-1",
            leaf_id=None,
            entries=entries,
            model="model-a",
        ),
        trace_records=({"event_type": "run_finished"},),
    )


def test_deterministic_validators_accept_exact_artifacts_and_preserved_sentinels(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sentinel.bin").write_bytes(b"\x00protected\xff")
    before = snapshot_workspace(workspace)
    (workspace / "result.json").write_text(
        '{"count":2,"items":["alpha","beta"]}\n',
        encoding="utf-8",
    )

    failures = evaluate_validators(
        _observation(workspace, before=before),
        (
            ExactJsonFile("result.json", {"count": 2, "items": ["alpha", "beta"]}),
            ExactWorkspaceDelta(created=("result.json",)),
            FilesUnchanged(("sentinel.bin",)),
        ),
    )

    assert failures == ()


def test_json_validator_reports_invalid_json_without_echoing_artifact_bytes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    (workspace / "result.json").write_text('{"api_key":"secret-value"', encoding="utf-8")

    failures = evaluate_validators(
        _observation(workspace, before=before),
        (ExactJsonFile("result.json", {"ok": True}),),
    )

    assert failures == ("result.json is not valid JSON",)
    assert "secret-value" not in failures[0]


def test_text_and_delta_validators_explain_minimal_postcondition_failures(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    (workspace / "answer.txt").write_text("almost\n", encoding="utf-8")
    (workspace / "unexpected.txt").write_text("extra\n", encoding="utf-8")

    failures = evaluate_validators(
        _observation(workspace, before=before),
        (
            ExactTextFile("answer.txt", "exact\n"),
            ExactWorkspaceDelta(created=("answer.txt",)),
        ),
    )

    assert failures == (
        "answer.txt text did not match the required content",
        "workspace created paths differed: expected [answer.txt], observed "
        "[answer.txt, unexpected.txt]",
    )


def test_durable_user_message_validator_uses_the_conversation_view(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    entries = (
        UserMessageEntry(id="user-1", content="first"),
        UserMessageEntry(id="user-2", parent_id="user-1", content="follow up"),
    )

    observation = _observation(workspace, before=before, entries=entries)

    assert (
        evaluate_validators(
            observation,
            (DurableUserMessages(("first", "follow up")),),
        )
        == ()
    )
    assert evaluate_validators(
        observation,
        (DurableUserMessages(("first", "different")),),
    ) == ("durable User messages did not match the ordered requests",)


def test_durable_validators_accept_tool_evidence_and_one_ordered_session_branch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    entries = (
        UserMessageEntry(id="user-1", content="first"),
        AssistantMessageEntry(
            id="assistant-tool-1",
            parent_id="user-1",
            tool_calls=[ToolCall("write-1", "write", {"path": "plan.txt"})],
        ),
        ToolResultEntry(
            id="tool-result-1",
            parent_id="assistant-tool-1",
            result=ToolResult("write-1", output="wrote plan.txt"),
        ),
        AssistantMessageEntry(
            id="assistant-final-1",
            parent_id="tool-result-1",
            content="created",
        ),
        UserMessageEntry(id="user-2", parent_id="assistant-final-1", content="follow up"),
        AssistantMessageEntry(
            id="assistant-tool-2",
            parent_id="user-2",
            tool_calls=[ToolCall("edit-1", "edit", {"path": "plan.txt"})],
        ),
        ToolResultEntry(
            id="tool-result-2",
            parent_id="assistant-tool-2",
            result=ToolResult("edit-1", output="", error="file_not_found: stale.txt"),
        ),
        AssistantMessageEntry(
            id="assistant-final-2",
            parent_id="tool-result-2",
            content="recovered",
        ),
    )
    after = snapshot_workspace(workspace)
    observation = EvaluationObservation(
        before=before,
        after=after,
        delta=compare_workspace_snapshots(before, after),
        run_statuses=(RunStatus.COMPLETED, RunStatus.COMPLETED),
        conversation=ConversationView(
            session_id="session-1",
            leaf_id="assistant-final-2",
            entries=entries,
            model="model-a",
        ),
        trace_records=(),
    )

    failures = evaluate_validators(
        observation,
        (
            DurableToolOutcome(
                "write",
                arguments=(("path", "plan.txt"),),
            ),
            DurableToolOutcome(
                "edit",
                arguments=(("path", "plan.txt"),),
                error_prefix="file_not_found:",
            ),
            DurableRequestSegments(("first", "follow up")),
            DurableSingleBranch(),
        ),
    )

    assert failures == ()


def test_durable_tool_outcome_requires_a_matching_paired_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    entries = (
        UserMessageEntry(id="user-1", content="task"),
        AssistantMessageEntry(
            id="assistant-1",
            parent_id="user-1",
            tool_calls=[ToolCall("read-1", "read", {"path": "different.txt"})],
        ),
        ToolResultEntry(
            id="result-1",
            parent_id="assistant-1",
            result=ToolResult("read-1", output="", error="file_not_found: different.txt"),
        ),
    )
    observation = _observation(workspace, before=before, entries=entries)

    failures = evaluate_validators(
        observation,
        (
            DurableToolOutcome(
                "read",
                arguments=(("path", "source/current.json"),),
                error_prefix="file_not_found:",
            ),
        ),
    )

    assert failures == ("durable Conversation View lacked the required read Tool outcome",)


def test_durable_tool_outcome_does_not_pair_reused_call_ids_across_runs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = snapshot_workspace(workspace)
    entries = (
        UserMessageEntry(id="user-1", content="first"),
        AssistantMessageEntry(
            id="assistant-1",
            parent_id="user-1",
            tool_calls=[ToolCall("reused-1", "read", {"path": "source/current.json"})],
        ),
        ToolResultEntry(
            id="result-1",
            parent_id="assistant-1",
            result=ToolResult("reused-1", output="current contents"),
        ),
        AssistantMessageEntry(id="final-1", parent_id="result-1", content="done"),
        UserMessageEntry(id="user-2", parent_id="final-1", content="second"),
        AssistantMessageEntry(
            id="assistant-2",
            parent_id="user-2",
            tool_calls=[ToolCall("reused-1", "read", {"path": "other.json"})],
        ),
        ToolResultEntry(
            id="result-2",
            parent_id="assistant-2",
            result=ToolResult("reused-1", output="", error="file_not_found: other.json"),
        ),
    )
    observation = _observation(workspace, before=before, entries=entries)

    failures = evaluate_validators(
        observation,
        (
            DurableToolOutcome(
                "read",
                arguments=(("path", "source/current.json"),),
                error_prefix="file_not_found:",
            ),
        ),
    )

    assert failures == ("durable Conversation View lacked the required read Tool outcome",)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "", "."])
def test_file_validators_reject_non_workspace_relative_paths(path: str) -> None:
    with pytest.raises(ValueError, match="workspace-relative"):
        ExactTextFile(path, "content")
