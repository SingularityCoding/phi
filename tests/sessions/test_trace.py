from __future__ import annotations

import json

from phi.model import ModelHTTPError, ModelResponse, ScriptedModel, ToolCall, Usage
from phi.sessions import (
    SessionStorage,
    create_session,
    materialize_conversation,
    resume_session,
    send_message,
)
from phi.settings import Settings
from phi.tools import (
    BYPASS_MODE,
    RuleBasedApprovalPolicy,
    ToolDispatcher,
    ToolRegistry,
    tool,
)


async def test_run_events_are_redacted_into_a_separate_trace_product(tmp_path) -> None:
    @tool(name="verify", description="Verify a credential-shaped argument.")
    async def verify(api_key: str) -> str:
        return f"accepted {len(api_key)} characters"

    storage = SessionStorage(tmp_path)
    tools = ToolRegistry([verify])
    handle = await create_session(storage, model="model-a")
    fake_secret = "sk-fake-secret-for-redaction"
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("call-1", "verify", {"api_key": fake_secret})]),
            ModelResponse(
                content="done",
                usage=Usage(prompt_tokens=3, completion_tokens=1, total_tokens=4),
            ),
        ]
    )

    updated, _ = await send_message(
        handle,
        "verify",
        storage=storage,
        settings=Settings(),
        model=model,
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=2,
    )

    trace_text = storage.trace_path(updated.session_id).read_text(encoding="utf-8")
    records = [json.loads(line) for line in trace_text.splitlines()]
    assert fake_secret not in trace_text
    assert "[REDACTED]" in trace_text
    assert '"prompt_tokens":3' in trace_text
    assert {record["schema_version"] for record in records} == {1}
    assert all(record["event_type"] and record["run_id"] for record in records)
    assert [record["event_index"] for record in records] == list(range(len(records)))

    storage.trace_path(updated.session_id).write_text("not-json\n", encoding="utf-8")
    resumed_view = await materialize_conversation(
        SessionStorage(tmp_path),
        await resume_session(SessionStorage(tmp_path), updated.session_id),
    )
    assert resumed_view.entries[-1].entry_type == "assistant_message"


async def test_provider_error_credentials_are_redacted_from_trace_text(tmp_path) -> None:
    storage = SessionStorage(tmp_path)
    tools = ToolRegistry()
    handle = await create_session(storage, model="model-a")
    plain_secret = "plain-fake-provider-secret"
    bearer_secret = "bearer-fake-provider-secret"

    await send_message(
        handle,
        "fail safely",
        storage=storage,
        settings=Settings(),
        model=ScriptedModel(
            [
                ModelHTTPError(
                    401,
                    (f'{{"api_key":"{plain_secret}"}}; Authorization: Bearer {bearer_secret}'),
                )
            ]
        ),
        model_info=None,
        tools=tools,
        dispatcher=ToolDispatcher(tools, RuleBasedApprovalPolicy(BYPASS_MODE)),
        stable_instructions="stable",
        max_steps=1,
    )

    trace_text = storage.trace_path(handle.session_id).read_text(encoding="utf-8")
    assert plain_secret not in trace_text
    assert bearer_secret not in trace_text
    assert "[REDACTED]" in trace_text
