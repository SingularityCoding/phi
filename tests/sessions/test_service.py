from __future__ import annotations

import pytest

from phi.harness import RunStatus
from phi.model import ModelInfo, ModelRequest, ModelResponse, ScriptedModel, ToolCall, Usage
from phi.sessions import (
    AssistantMessageEntry,
    InvalidSessionLeafError,
    SessionStorage,
    ToolResultEntry,
    UserMessageEntry,
    create_session,
    fork_session,
    inspect_context,
    list_leaves,
    list_sessions,
    materialize_conversation,
    rename_session,
    resume_session,
    select_model,
    send_message,
    switch_leaf,
)
from phi.settings import Settings
from phi.tools import BYPASS_MODE, RuleBasedApprovalPolicy, ToolDispatcher, ToolRegistry


async def test_session_identity_and_name_survive_a_fresh_storage_instance(tmp_path) -> None:
    storage = SessionStorage(tmp_path)

    created = await create_session(storage, model="model-a")
    renamed = await rename_session(storage, created, "Research thread")

    fresh_storage = SessionStorage(tmp_path)
    resumed = await resume_session(fresh_storage, created.session_id)
    sessions = await list_sessions(fresh_storage)

    assert resumed.session_id == created.session_id
    assert resumed.leaf_id is None
    assert resumed.metadata.name == "Research thread"
    assert resumed.metadata.model == "model-a"
    assert resumed.prompt_budget_anchor is None
    assert sessions == [resumed.metadata]
    assert renamed.revision == resumed.revision


async def test_completed_message_is_durable_and_resumes_with_the_exact_context(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    created = await create_session(storage, model="model-a")
    model = ScriptedModel(
        [
            ModelResponse(
                content="Hello back",
                finish_reason="stop",
                usage=Usage(prompt_tokens=17, completion_tokens=2, total_tokens=19),
            )
        ]
    )
    tools = ToolRegistry()

    updated, result = await send_message(
        created,
        "Hello",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="Always be exact.",
        max_steps=2,
    )

    resumed = await resume_session(SessionStorage(tmp_path), created.session_id)
    view = await materialize_conversation(SessionStorage(tmp_path), resumed)

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Hello back"
    assert [type(entry) for entry in view.entries] == [
        UserMessageEntry,
        AssistantMessageEntry,
    ]
    user_entry, assistant_entry = view.entries
    assert isinstance(user_entry, UserMessageEntry)
    assert isinstance(assistant_entry, AssistantMessageEntry)
    assert user_entry.content == "Hello"
    assert assistant_entry.content == "Hello back"
    assert resumed.leaf_id == updated.leaf_id == view.entries[-1].id
    assert resumed.prompt_budget_anchor is None
    assert updated.prompt_budget_anchor is not None
    assert updated.prompt_budget_anchor.prompt_tokens == 17
    assert model.requests == [
        ModelRequest(
            messages=[
                {"role": "system", "content": "Always be exact."},
                {"role": "user", "content": "Hello"},
            ],
            tools=[],
            model="model-a",
        )
    ]


async def test_missing_usage_clears_the_runtime_prompt_anchor(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    handle, _ = await send_message(
        handle,
        "anchored",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel(
            [
                ModelResponse(
                    content="with usage",
                    usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
                )
            ]
        ),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    assert handle.prompt_budget_anchor is not None

    handle, _ = await send_message(
        handle,
        "unanchored",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="without usage")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )

    assert handle.prompt_budget_anchor is None


async def test_model_selection_updates_empty_branch_and_implicitly_forks_after_output(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    empty = await create_session(storage, model="model-a")

    changed_empty = await select_model(storage, empty, "model-b")

    assert changed_empty.session_id == empty.session_id
    assert changed_empty.metadata.model == "model-b"
    tools = ToolRegistry()
    completed, _ = await send_message(
        changed_empty,
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

    changed_output = await select_model(storage, completed, "model-c")
    original = await resume_session(storage, completed.session_id)

    assert changed_output.session_id != completed.session_id
    assert changed_output.metadata.origin == "fork"
    assert changed_output.metadata.model == "model-c"
    assert changed_output.metadata.fork_point_entry_id == completed.leaf_id
    assert changed_output.prompt_budget_anchor is None
    assert original.metadata.model == "model-b"


async def test_model_selection_updates_an_explicit_fork_before_its_first_local_output(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    root = await create_session(storage, model="model-a")
    root, _ = await send_message(
        root,
        "root",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="root answer")]),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )
    assert root.leaf_id is not None
    forked = await fork_session(storage, root, root.leaf_id, model="model-b")

    changed = await select_model(storage, forked, "model-c")

    assert changed.session_id == forked.session_id
    assert changed.metadata.model == "model-c"


async def test_context_inspection_uses_the_send_builder_without_calling_the_model(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a")
    model = ScriptedModel(
        [
            ModelResponse(
                content="answer",
                usage=Usage(prompt_tokens=20, completion_tokens=1, total_tokens=21),
            )
        ]
    )
    handle, _ = await send_message(
        handle,
        "question",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )

    inspection = await inspect_context(
        storage,
        handle,
        settings=Settings(),
        model_info=ModelInfo("model-a", max_input_tokens=20_000),
        tools=tools,
        stable_instructions="stable",
    )

    assert len(model.requests) == 1
    assert inspection.request.messages == [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert inspection.context.character_counts["system_prompt"] == 6
    assert inspection.estimate.used_provider_anchor is True
    assert inspection.effective_input_limit == 20_000


async def test_switching_an_entry_creates_navigable_sibling_leaves(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    handle = await create_session(storage, model="model-a")
    handle, _ = await send_message(
        handle,
        "root",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="root answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    branch_point = handle.leaf_id
    assert branch_point is not None
    handle, _ = await send_message(
        handle,
        "main",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="main answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    main_leaf = handle.leaf_id
    assert main_leaf is not None

    at_branch = await switch_leaf(storage, handle, branch_point)
    assert at_branch.prompt_budget_anchor is None
    branched, _ = await send_message(
        at_branch,
        "branch",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="branch answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )

    assert set(await list_leaves(storage, branched)) == {main_leaf, branched.leaf_id}
    selected_main = await switch_leaf(storage, branched, main_leaf)
    main_view = await materialize_conversation(storage, selected_main)
    assert [
        entry.content
        for entry in main_view.entries
        if isinstance(entry, (UserMessageEntry, AssistantMessageEntry))
    ] == ["root", "root answer", "main", "main answer"]


async def test_fork_references_an_exact_parent_entry_without_copying_history(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    root = await create_session(storage, model="model-a")

    root, _ = await send_message(
        root,
        "first",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="first answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    first_path = await materialize_conversation(storage, root)
    fork_point = first_path.entries[-1].id
    root, _ = await send_message(
        root,
        "second",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="second answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )

    forked = await fork_session(storage, root, fork_point)

    assert forked.metadata.parent_session_id == root.session_id
    assert forked.metadata.fork_point_entry_id == fork_point
    assert forked.session_file.read_text(encoding="utf-8") == ""

    child_model = ScriptedModel([ModelResponse(content="alternate answer")])
    forked, _ = await send_message(
        forked,
        "alternate",
        storage=storage,
        settings=Settings(),
        model=child_model,
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )

    assert [message["content"] for message in child_model.requests[0].messages] == [
        "stable",
        "first",
        "first answer",
        "alternate",
    ]
    root_entries = (await materialize_conversation(storage, root)).entries
    assert [
        entry.content
        for entry in root_entries
        if isinstance(entry, (UserMessageEntry, AssistantMessageEntry))
    ] == [
        "first",
        "first answer",
        "second",
        "second answer",
    ]


async def test_nested_forks_cross_each_exact_parent_and_reject_unreachable_points(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    root = await create_session(storage, model="model-a")
    root, _ = await send_message(
        root,
        "root",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="root answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    with pytest.raises(InvalidSessionLeafError):
        await fork_session(storage, root, "unreachable")
    assert root.leaf_id is not None
    child = await fork_session(storage, root, root.leaf_id)
    child, _ = await send_message(
        child,
        "child",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="child answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    assert child.leaf_id is not None
    grandchild = await fork_session(storage, child, child.leaf_id)

    view = await materialize_conversation(storage, grandchild)
    assert grandchild.session_file.read_text(encoding="utf-8") == ""
    assert [
        entry.content
        for entry in view.entries
        if isinstance(entry, (UserMessageEntry, AssistantMessageEntry))
    ] == ["root", "root answer", "child", "child answer"]


async def test_fork_and_leaf_switch_require_complete_tool_exchange_boundaries(
    tmp_path,
) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a")
    handle, result = await send_message(
        handle,
        "use both",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall("one", "missing-one", {}),
                        ToolCall("two", "missing-two", {}),
                    ]
                )
            ]
        ),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )
    assert result.status is RunStatus.MAX_STEPS
    view = await materialize_conversation(storage, handle)
    assistant = view.entries[1]
    first_result = view.entries[2]
    final_result = view.entries[3]
    assert isinstance(assistant, AssistantMessageEntry)
    assert isinstance(first_result, ToolResultEntry)
    assert isinstance(final_result, ToolResultEntry)

    for unsafe_entry in (assistant, first_result):
        with pytest.raises(InvalidSessionLeafError):
            await fork_session(storage, handle, unsafe_entry.id)
        with pytest.raises(InvalidSessionLeafError):
            await switch_leaf(storage, handle, unsafe_entry.id)

    forked = await fork_session(storage, handle, final_result.id)
    switched = await switch_leaf(storage, handle, final_result.id)
    assert forked.metadata.fork_point_entry_id == final_result.id
    assert switched.leaf_id == final_result.id


async def test_subagent_lineage_does_not_inherit_the_parent_conversation(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    dispatcher = ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE))
    parent = await create_session(storage, model="model-a")
    parent, _ = await send_message(
        parent,
        "private parent history",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel([ModelResponse(content="parent answer")]),
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )
    child = await create_session(
        storage,
        model="model-a",
        origin="subagent",
        parent_session_id=parent.session_id,
    )
    child_model = ScriptedModel([ModelResponse(content="child answer")])

    child, _ = await send_message(
        child,
        "delegated task",
        storage=storage,
        settings=Settings(),
        model=child_model,
        model_info=None,
        tools=tools,
        dispatcher=dispatcher,
        stable_instructions="stable",
        max_steps=1,
    )

    assert child.metadata.parent_session_id == parent.session_id
    assert child.metadata.fork_point_entry_id is None
    assert child_model.requests[0].messages == [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "delegated task"},
    ]
