from __future__ import annotations

import json

import pytest

from phi.harness import (
    CompactionDisabledError,
    ContextCapacityError,
    InvalidCompactionSummaryError,
    RunStatus,
)
from phi.model import (
    ModelContextLimitError,
    ModelInfo,
    ModelResponse,
    ScriptedModel,
    ToolCall,
    Usage,
)
from phi.sessions import (
    AssistantMessageEntry,
    CompactionEntry,
    CorruptSessionError,
    SessionStorage,
    create_session,
    manual_compact,
    materialize_conversation,
    resume_session,
    send_message,
    switch_leaf,
)
from phi.settings import Settings
from phi.tools import BYPASS_MODE, RuleBasedApprovalPolicy, ToolDispatcher, ToolRegistry, tool


async def test_manual_compaction_summarizes_without_tools_or_deleting_old_entries(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("one", "answer one"), ("two", "answer two")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel(
                [
                    ModelResponse(
                        content=answer,
                        usage=Usage(prompt_tokens=25, completion_tokens=2, total_tokens=27),
                    )
                ]
            ),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    records_before = handle.session_file.read_text(encoding="utf-8").splitlines()
    summarizer = ScriptedModel([ModelResponse(content="Earlier, the user discussed one.")])
    settings = Settings(
        compaction_keep_recent_tokens=0,
        compaction_summary_max_tokens=100,
    )

    compacted = await manual_compact(
        handle,
        storage=storage,
        settings=settings,
        model=summarizer,
        model_info=ModelInfo("model-a", max_output_tokens=40),
        tools=tools,
        stable_instructions="stable",
        focus="decisions",
    )

    assert summarizer.requests[0].tools == []
    assert summarizer.requests[0].max_tokens == 40
    assert "decisions" in summarizer.requests[0].messages[-1]["content"]
    view = await materialize_conversation(storage, compacted)
    assert view.dropped_summary == "Earlier, the user discussed one."
    assert len(view.entries) == 1
    assert isinstance(view.entries[0], AssistantMessageEntry)
    assert view.entries[0].content == "answer two"
    records_after = compacted.session_file.read_text(encoding="utf-8").splitlines()
    assert records_after[: len(records_before)] == records_before
    assert len(records_after) == len(records_before) + 1
    state = await storage.load_state(compacted.session_id)
    assert isinstance(state.entries[-1], CompactionEntry)
    assert state.entries[-1].tokens_before_source == "estimate"


async def test_compaction_provenance_cannot_reference_a_descendant_entry(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("one", "answer one"), ("two", "answer two")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    handle = await manual_compact(
        handle,
        storage=storage,
        settings=Settings(compaction_keep_recent_tokens=0),
        model=ScriptedModel([ModelResponse(content="summary")]),
        model_info=None,
        tools=tools,
        stable_instructions="stable",
    )
    handle, _ = await send_message(
        handle,
        "after compaction",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="later answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    lines = handle.session_file.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        record = json.loads(line)
        if record["entry_type"] == "compaction":
            record["first_kept_entry_id"] = handle.leaf_id
            lines[index] = json.dumps(record)
            break
    handle.session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(CorruptSessionError, match="retained Entry"):
        await resume_session(SessionStorage(tmp_path), handle.session_id)


async def test_resume_validates_compaction_provenance_outside_the_selected_path(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("one", "answer one"), ("two", "answer two")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    selected_ancestor = (await materialize_conversation(storage, handle)).entries[1].id
    compacted = await manual_compact(
        handle,
        storage=storage,
        settings=Settings(compaction_keep_recent_tokens=0),
        model=ScriptedModel([ModelResponse(content="summary")]),
        model_info=None,
        tools=tools,
        stable_instructions="stable",
    )
    selected = await switch_leaf(storage, compacted, selected_ancestor)
    lines = selected.session_file.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        record = json.loads(line)
        if record["entry_type"] == "compaction":
            record["first_kept_entry_id"] = "missing-retained-entry"
            lines[index] = json.dumps(record)
            break
    selected.session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(CorruptSessionError, match="retained Entry"):
        await resume_session(SessionStorage(tmp_path), selected.session_id)


async def test_actual_summary_must_fit_before_compaction_is_committed(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("one", "answer one"), ("two", "answer two")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )

    with pytest.raises(ContextCapacityError, match="rebuilt Context"):
        await manual_compact(
            handle,
            storage=storage,
            settings=Settings(
                compaction_reserve_tokens=0,
                compaction_keep_recent_tokens=0,
                compaction_summary_max_tokens=20,
            ),
            model=ScriptedModel([ModelResponse(content="s" * 2_000)]),
            model_info=ModelInfo("model-a", max_input_tokens=400, max_output_tokens=20),
            tools=tools,
            stable_instructions="stable",
        )

    state = await storage.load_state(handle.session_id)
    assert not any(isinstance(entry, CompactionEntry) for entry in state.entries)


async def test_send_message_compacts_once_before_a_known_oversized_request(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("x" * 300, "y" * 300), ("short", "brief")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    model = ScriptedModel(
        [
            ModelResponse(content="summary of the long exchange"),
            ModelResponse(content="final answer"),
        ]
    )
    settings = Settings(
        compaction_reserve_tokens=180,
        compaction_keep_recent_tokens=0,
        compaction_summary_max_tokens=20,
    )

    updated, result = await send_message(
        handle,
        "new",
        storage=storage,
        settings=settings,
        model=model,
        model_info=ModelInfo("model-a", max_input_tokens=400, max_output_tokens=30),
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "final answer"
    assert len(model.requests) == 2
    assert model.requests[0].tools == []
    assert model.requests[0].max_tokens == 20
    assert [message["content"] for message in model.requests[1].messages] == [
        "stable",
        "Dropped conversation history summary:\nsummary of the long exchange",
        "brief",
        "new",
    ]
    state = await storage.load_state(updated.session_id)
    assert sum(isinstance(entry, CompactionEntry) for entry in state.entries) == 1


async def test_overflow_recovery_is_refused_after_a_completed_tool_call(tmp_path) -> None:
    effects: list[str] = []

    @tool(name="act", description="Perform one visible effect.")
    async def act() -> str:
        effects.append("acted")
        return "done"

    storage = SessionStorage(tmp_path)
    tools = ToolRegistry([act])
    handle = await create_session(storage, model="model-a")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("act-1", "act", {})]),
            ModelContextLimitError(400, '{"error":{"code":"context_length_exceeded"}}'),
            ModelResponse(content="must not be consumed"),
        ]
    )

    updated, result = await send_message(
        handle,
        "act then continue",
        storage=storage,
        settings=Settings(compaction_keep_recent_tokens=0),
        model=model,
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=2,
    )

    assert result.status is RunStatus.FAILED
    assert effects == ["acted"]
    assert len(model.requests) == 2
    state = await storage.load_state(updated.session_id)
    assert not any(isinstance(entry, CompactionEntry) for entry in state.entries)


async def test_repeated_compaction_includes_the_previous_summary_and_rejects_invalid_output(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("one", "answer one"), ("two", "answer two")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    settings = Settings(compaction_keep_recent_tokens=0)
    handle = await manual_compact(
        handle,
        storage=storage,
        settings=settings,
        model=ScriptedModel([ModelResponse(content="summary one")]),
        model_info=None,
        tools=tools,
        stable_instructions="stable",
    )
    handle, _ = await send_message(
        handle,
        "three",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="answer three")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    second_summarizer = ScriptedModel([ModelResponse(content="summary two")])

    handle = await manual_compact(
        handle,
        storage=storage,
        settings=settings,
        model=second_summarizer,
        model_info=None,
        tools=tools,
        stable_instructions="stable",
    )

    assert (
        "Previous dropped-history summary:\nsummary one"
        in (second_summarizer.requests[0].messages[-1]["content"])
    )
    assert (await materialize_conversation(storage, handle)).dropped_summary == "summary two"

    with pytest.raises(CompactionDisabledError):
        await manual_compact(
            handle,
            storage=storage,
            settings=Settings(compaction_enabled=False),
            model=ScriptedModel([]),
            model_info=None,
            tools=tools,
            stable_instructions="stable",
        )

    handle, _ = await send_message(
        handle,
        "four",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="answer four")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    with pytest.raises(InvalidCompactionSummaryError):
        await manual_compact(
            handle,
            storage=storage,
            settings=settings,
            model=ScriptedModel([ModelResponse(tool_calls=[ToolCall("bad", "not-allowed", {})])]),
            model_info=None,
            tools=tools,
            stable_instructions="stable",
        )


async def test_oversized_summary_input_is_rejected_before_calling_the_model(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    for user_text, answer in (("x" * 1_000, "y" * 1_000), ("short", "brief")):
        handle, _ = await send_message(
            handle,
            user_text,
            storage=storage,
            settings=Settings(),
            model=ScriptedModel([ModelResponse(content=answer)]),
            model_info=None,
            tools=tools,
            dispatcher=dispatcher,
            stable_instructions="stable",
            max_steps=1,
        )
    summarizer = ScriptedModel([ModelResponse(content="must not be used")])

    with pytest.raises(ContextCapacityError, match="summary request"):
        await manual_compact(
            handle,
            storage=storage,
            settings=Settings(
                compaction_reserve_tokens=0,
                compaction_keep_recent_tokens=0,
                compaction_summary_max_tokens=20,
            ),
            model=summarizer,
            model_info=ModelInfo("model-a", max_input_tokens=200),
            tools=tools,
            stable_instructions="stable",
        )

    assert summarizer.requests == []


async def test_irreducible_first_context_returns_a_capacity_error_without_a_model_call(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a")
    model = ScriptedModel([ModelResponse(content="must not be used")])

    with pytest.raises(ContextCapacityError, match="nothing can be compacted"):
        await send_message(
            handle,
            "new",
            storage=storage,
            settings=Settings(
                compaction_reserve_tokens=0,
                compaction_keep_recent_tokens=0,
                compaction_summary_max_tokens=20,
            ),
            model=model,
            model_info=ModelInfo("model-a", max_input_tokens=200),
            tools=tools,
            dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
            stable_instructions="x" * 1_000,
            max_steps=1,
        )

    assert model.requests == []
    state = await storage.load_state(handle.session_id)
    assert len(state.entries) == 1


async def test_context_limit_failure_compacts_and_retries_only_once_without_rewriting_user_entry(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    handle, _ = await send_message(
        handle,
        "old",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="old answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    model = ScriptedModel(
        [
            ModelContextLimitError(400, '{"error":{"code":"context_length_exceeded"}}'),
            ModelResponse(content="old exchange summary"),
            ModelResponse(content="recovered"),
        ]
    )

    updated, result = await send_message(
        handle,
        "new",
        storage=storage,
        settings=Settings(
            compaction_keep_recent_tokens=0,
            compaction_summary_max_tokens=20,
        ),
        model=model,
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "recovered"
    assert len(model.requests) == 3
    state = await storage.load_state(updated.session_id)
    assert (
        sum(
            entry.entry_type == "user_message" and entry.content == "new"
            for entry in state.entries
            if entry.entry_type == "user_message"
        )
        == 1
    )
    assert sum(isinstance(entry, CompactionEntry) for entry in state.entries) == 1
