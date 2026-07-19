from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from phi.model import ModelResponse, ScriptedModel
from phi.sessions import (
    CorruptSessionError,
    IncompatibleSessionVersionError,
    InvalidSessionLeafError,
    MissingEntryParentError,
    SessionLineageCycleError,
    SessionNotFoundError,
    SessionStorage,
    StaleSessionHandleError,
    create_session,
    list_sessions,
    materialize_conversation,
    rename_session,
    resume_session,
    send_message,
)
from phi.settings import Settings
from phi.tools import BYPASS_MODE, RuleBasedApprovalPolicy, ToolDispatcher, ToolRegistry


async def test_create_collision_preserves_the_existing_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(tmp_path)
    existing = await create_session(storage, model="existing-model")
    journal_before = existing.session_file.read_bytes()
    metadata_before = storage.metadata_path(existing.session_id).read_bytes()

    def collide_uuid() -> UUID:
        return UUID(existing.session_id)

    monkeypatch.setattr("phi.sessions.storage.uuid4", collide_uuid)

    with pytest.raises(CorruptSessionError, match="generated identity already exists"):
        await create_session(storage, model="replacement-model")

    assert existing.session_file.read_bytes() == journal_before
    assert storage.metadata_path(existing.session_id).read_bytes() == metadata_before


async def _completed_session(storage: SessionStorage):
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a")
    handle, _ = await send_message(
        handle,
        "hello",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="hi")]),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )
    return handle


async def test_resume_ignores_an_uncommitted_incomplete_trailing_record(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    handle = await _completed_session(storage)
    trailing = b'{"schema_version":1,"entry_type":"user_message"'
    with handle.session_file.open("ab") as file:
        file.write(trailing)

    resumed = await resume_session(SessionStorage(tmp_path), handle.session_id)
    view = await materialize_conversation(SessionStorage(tmp_path), resumed)

    assert "ignored 1 uncommitted trailing Entry record(s)" in resumed.diagnostics
    assert len(view.entries) == 2

    tools = ToolRegistry()
    continued, result = await send_message(
        resumed,
        "continue",
        storage=SessionStorage(tmp_path),
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="continued")]),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )
    fresh = await resume_session(SessionStorage(tmp_path), continued.session_id)
    continued_view = await materialize_conversation(SessionStorage(tmp_path), fresh)

    assert result.output == "continued"
    assert [entry.entry_type for entry in continued_view.entries] == [
        "user_message",
        "assistant_message",
        "user_message",
        "assistant_message",
    ]
    assert continued.session_file.read_bytes().endswith(trailing)
    assert "ignored 1 uncommitted trailing Entry record(s)" in fresh.diagnostics


async def test_untrusted_session_ids_cannot_escape_the_storage_root(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    with pytest.raises(SessionNotFoundError):
        storage.metadata_path("../outside")
    with pytest.raises(SessionNotFoundError):
        await resume_session(storage, "../outside")


async def test_unknown_committed_entry_version_fails_without_shape_guessing(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    handle = await _completed_session(storage)
    lines = handle.session_file.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["schema_version"] = 2
    lines[0] = json.dumps(first)
    handle.session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(IncompatibleSessionVersionError):
        await resume_session(SessionStorage(tmp_path), handle.session_id)


async def test_stale_handle_cannot_overwrite_a_newer_metadata_revision(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    original = await create_session(storage)
    newer = await rename_session(storage, original, "newer")

    with pytest.raises(StaleSessionHandleError):
        await rename_session(storage, original, "stale")

    assert (await resume_session(storage, original.session_id)).metadata.name == "newer"
    assert newer.revision == original.revision + 1


async def test_metadata_leaf_cannot_reference_an_uncommitted_entry(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    handle = await _completed_session(storage)
    metadata_path = storage.metadata_path(handle.session_id)
    envelope = json.loads(metadata_path.read_text(encoding="utf-8"))
    envelope["metadata"]["leaf_id"] = "uncommitted-entry"
    metadata_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(InvalidSessionLeafError):
        await resume_session(SessionStorage(tmp_path), handle.session_id)


async def test_missing_parents_and_cycles_are_typed_materialization_failures(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    missing = await _completed_session(storage)
    missing_lines = missing.session_file.read_text(encoding="utf-8").splitlines()
    missing_first = json.loads(missing_lines[0])
    missing_first["parent_id"] = "absent"
    missing_lines[0] = json.dumps(missing_first)
    missing.session_file.write_text("\n".join(missing_lines) + "\n", encoding="utf-8")

    with pytest.raises(MissingEntryParentError):
        await resume_session(SessionStorage(tmp_path), missing.session_id)
    with pytest.raises(MissingEntryParentError):
        await list_sessions(SessionStorage(tmp_path))

    cycle = await _completed_session(storage)
    cycle_lines = cycle.session_file.read_text(encoding="utf-8").splitlines()
    first = json.loads(cycle_lines[0])
    second = json.loads(cycle_lines[1])
    first["parent_id"] = second["id"]
    cycle_lines[0] = json.dumps(first)
    cycle.session_file.write_text("\n".join(cycle_lines) + "\n", encoding="utf-8")

    with pytest.raises(SessionLineageCycleError):
        await resume_session(SessionStorage(tmp_path), cycle.session_id)
