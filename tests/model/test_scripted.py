import pytest

from phi.model import (
    ContentDelta,
    FinishEvent,
    ModelRequest,
    ModelResponse,
    ScriptedModel,
    Usage,
    UsageEvent,
)


async def test_scripted_model_records_requests_and_never_repeats_exhausted_script():
    first = ModelResponse(content="first", finish_reason="stop", raw={"id": "first"})
    second = ModelResponse(content="second", finish_reason="length", raw={"id": "second"})
    model = ScriptedModel([first, second])
    first_request = ModelRequest(messages=[{"role": "user", "content": "one"}])
    second_request = ModelRequest(messages=[{"role": "user", "content": "two"}])
    exhausted_request = ModelRequest(messages=[{"role": "user", "content": "three"}])

    assert await model.request(first_request) == first
    assert await model.request(second_request) == second
    with pytest.raises(RuntimeError, match="exhausted"):
        await model.request(exhausted_request)

    assert model.requests == [first_request, second_request, exhausted_request]


async def test_scripted_model_assembles_an_event_sequence_into_a_response():
    usage = Usage(prompt_tokens=2, completion_tokens=1, total_tokens=3)
    finish_raw = {"choices": [{"finish_reason": "stop"}]}
    usage_raw = {"choices": [], "usage": {}}
    model = ScriptedModel(
        [
            [
                ContentDelta("scripted"),
                FinishEvent(finish_reason="stop", raw=finish_raw),
                UsageEvent(usage=usage, raw=usage_raw),
            ]
        ]
    )
    request = ModelRequest(messages=[])

    response = await model.request(request)

    assert response == ModelResponse(
        content="scripted",
        usage=usage,
        finish_reason="stop",
        raw=usage_raw,
    )
    assert model.requests == [request]
