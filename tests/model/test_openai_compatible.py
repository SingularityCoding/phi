import json
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from phi.model import (
    ContentDelta,
    FinishEvent,
    ModelConfig,
    ModelHTTPError,
    ModelProtocolError,
    ModelRequest,
    ModelResponse,
    ModelTimeoutError,
    OpenAICompatibleModel,
    ReasoningDelta,
    ResponseAssembler,
    ToolCall,
    ToolCallDelta,
    ToolResult,
    Usage,
    UsageEvent,
    serialize_assistant_response,
    serialize_tool_result,
)


def model_config() -> ModelConfig:
    return ModelConfig(
        base_url="https://proxy.example/v1",
        api_key=SecretStr("test-key"),
        default_model="course-model",
        request_timeout_seconds=30.0,
    )


async def test_non_streaming_request_serializes_and_normalizes_response():
    captured_payload = None
    raw_response = {
        "choices": [
            {
                "message": {
                    "content": "Forecast ready",
                    "reasoning_content": "I should check the weather",
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"Shanghai"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 7,
            "total_tokens": 27,
            "prompt_tokens_details": {"cached_tokens": 8},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(200, json=raw_response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleModel(model_config(), client=client)
        response = await model.request(
            ModelRequest(
                messages=[{"role": "user", "content": "Weather?"}],
                tools=[{"type": "function", "function": {"name": "weather"}}],
                temperature=0.2,
                max_tokens=100,
            )
        )

    assert captured_payload == {
        "model": "course-model",
        "messages": [{"role": "user", "content": "Weather?"}],
        "tools": [{"type": "function", "function": {"name": "weather"}}],
        "temperature": 0.2,
        "max_tokens": 100,
        "stream": False,
    }
    assert response.content == "Forecast ready"
    assert response.reasoning == "I should check the weather"
    assert response.tool_calls == [
        ToolCall(id="call_weather", name="weather", arguments={"city": "Shanghai"})
    ]
    assert response.usage == Usage(
        prompt_tokens=20,
        completion_tokens=7,
        total_tokens=27,
        cached_tokens=8,
        reasoning_tokens=3,
    )
    assert response.finish_reason == "tool_calls"
    assert response.raw == raw_response


async def test_streaming_request_emits_deltas_and_trailing_usage_through_done():
    captured_payload = None
    first_chunk = {
        "choices": [
            {
                "delta": {"reasoning_content": "check ", "content": "Hel"},
                "finish_reason": None,
            }
        ]
    }
    tool_chunk = {
        "choices": [
            {
                "delta": {
                    "content": "lo",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_read",
                            "function": {"name": "read_file", "arguments": '{"path":'},
                        },
                        {
                            "index": 1,
                            "id": "call_weather",
                            "function": {"name": "weather", "arguments": '{"city":'},
                        },
                    ],
                },
                "finish_reason": None,
            }
        ]
    }
    finish_chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '"README.md"}'}},
                        {"index": 1, "function": {"arguments": '"Shanghai"}'}},
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    usage_chunk = {
        "choices": [{"delta": {}, "finish_reason": None}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
    }
    body = (
        "".join(
            f"data: {json.dumps(chunk)}\n\n"
            for chunk in (first_chunk, tool_chunk, finish_chunk, usage_chunk)
        )
        + "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleModel(model_config(), client=client)
        events = [
            event
            async for event in model.request_stream(
                ModelRequest(messages=[{"role": "user", "content": "Hello"}])
            )
        ]

    assert captured_payload == {
        "model": "course-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert events == [
        ReasoningDelta("check "),
        ContentDelta("Hel"),
        ContentDelta("lo"),
        ToolCallDelta(
            index=0,
            id="call_read",
            name="read_file",
            arguments_fragment='{"path":',
        ),
        ToolCallDelta(
            index=1,
            id="call_weather",
            name="weather",
            arguments_fragment='{"city":',
        ),
        ToolCallDelta(index=0, arguments_fragment='"README.md"}'),
        ToolCallDelta(index=1, arguments_fragment='"Shanghai"}'),
        FinishEvent(finish_reason="tool_calls", raw=finish_chunk),
        UsageEvent(
            usage=Usage(prompt_tokens=12, completion_tokens=6, total_tokens=18),
            raw=usage_chunk,
        ),
    ]


@pytest.mark.parametrize("include_null", [False, True])
async def test_missing_or_null_usage_is_normalized_to_none(include_null: bool):
    raw_response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    if include_null:
        raw_response["usage"] = None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw_response))
    ) as client:
        response = await OpenAICompatibleModel(model_config(), client=client).request(
            ModelRequest(messages=[])
        )

    assert response.usage is None


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
        {"prompt_tokens": 1, "completion_tokens": "1", "total_tokens": 2},
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": True},
        {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "prompt_tokens_details": [],
        },
        {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "completion_tokens_details": {"reasoning_tokens": -1},
        },
    ],
)
async def test_malformed_supplied_usage_is_a_protocol_error(usage: object):
    raw_response = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": usage,
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw_response))
    ) as client:
        with pytest.raises(ModelProtocolError):
            await OpenAICompatibleModel(model_config(), client=client).request(
                ModelRequest(messages=[])
            )


async def test_invalid_tool_call_json_is_a_protocol_error():
    raw_response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "function": {"name": "broken", "arguments": "{not-json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw_response))
    ) as client:
        with pytest.raises(ModelProtocolError, match="invalid JSON"):
            await OpenAICompatibleModel(model_config(), client=client).request(
                ModelRequest(messages=[])
            )


@pytest.mark.parametrize(
    "raw_response",
    [
        {"choices": []},
        {"choices": {}},
        {"choices": [{"message": None, "finish_reason": "stop"}]},
    ],
)
async def test_malformed_success_response_is_a_protocol_error(raw_response: object):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw_response))
    ) as client:
        with pytest.raises(ModelProtocolError):
            await OpenAICompatibleModel(model_config(), client=client).request(
                ModelRequest(messages=[])
            )


async def test_empty_visible_content_with_length_finish_is_valid():
    raw_response = {
        "choices": [
            {
                "message": {"content": "", "reasoning_content": "budget used"},
                "finish_reason": "length",
            }
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw_response))
    ) as client:
        response = await OpenAICompatibleModel(model_config(), client=client).request(
            ModelRequest(messages=[])
        )

    assert response.content == ""
    assert response.reasoning == "budget used"
    assert response.finish_reason == "length"


async def test_unknown_finish_reason_is_preserved():
    raw_response = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "provider_specific_reason"}]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw_response))
    ) as client:
        response = await OpenAICompatibleModel(model_config(), client=client).request(
            ModelRequest(messages=[])
        )

    assert response.finish_reason == "provider_specific_reason"


async def test_http_error_retains_status_and_body():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "invalid virtual key"})
        )
    ) as client:
        with pytest.raises(ModelHTTPError) as raised:
            await OpenAICompatibleModel(model_config(), client=client).request(
                ModelRequest(messages=[])
            )

    assert raised.value.status_code == 401
    assert raised.value.body == '{"error":"invalid virtual key"}'


async def test_timeout_is_translated_to_model_timeout_error():
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(time_out)) as client:
        with pytest.raises(ModelTimeoutError):
            await OpenAICompatibleModel(model_config(), client=client).request(
                ModelRequest(messages=[])
            )


async def test_streaming_and_non_streaming_assemble_the_same_normalized_fields():
    usage = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    non_streaming = {
        "choices": [
            {
                "message": {"content": "answer", "reasoning_content": "think"},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
    content_chunk = {
        "choices": [
            {
                "delta": {"content": "answer", "reasoning_content": "think"},
                "finish_reason": None,
            }
        ]
    }
    finish_chunk = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    usage_chunk = {"choices": [], "usage": usage}
    streaming_body = (
        "".join(
            f"data: {json.dumps(chunk)}\n\n" for chunk in (content_chunk, finish_chunk, usage_chunk)
        )
        + "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["stream"]:
            return httpx.Response(200, text=streaming_body)
        return httpx.Response(200, json=non_streaming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleModel(model_config(), client=client)
        complete = await model.request(ModelRequest(messages=[]))
        assembler = ResponseAssembler()
        async for event in model.request_stream(ModelRequest(messages=[])):
            assembler.absorb(event)
        streamed = assembler.build()

    assert replace(streamed, raw={}) == replace(complete, raw={})
    assert streamed.raw == usage_chunk


async def test_streaming_repeated_tool_identity_is_only_emitted_on_first_fragment():
    first_chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_one",
                            "function": {"name": "tool", "arguments": "{"},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
    repeated_chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_one",
                            "function": {"name": "tool", "arguments": "}"},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    body = (
        f"data: {json.dumps(first_chunk)}\n\ndata: {json.dumps(repeated_chunk)}\n\ndata: [DONE]\n\n"
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        events = [
            event
            async for event in OpenAICompatibleModel(model_config(), client=client).request_stream(
                ModelRequest(messages=[])
            )
        ]

    assert events[:2] == [
        ToolCallDelta(
            index=0,
            id="call_one",
            name="tool",
            arguments_fragment="{",
        ),
        ToolCallDelta(index=0, arguments_fragment="}"),
    ]


async def test_stream_without_finish_or_usage_is_a_protocol_error():
    content_chunk = {"choices": [{"delta": {"content": "unfinished"}, "finish_reason": None}]}
    body = f"data: {json.dumps(content_chunk)}\n\ndata: [DONE]\n\n"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        with pytest.raises(ModelProtocolError, match="finish or usage"):
            events = OpenAICompatibleModel(model_config(), client=client).request_stream(
                ModelRequest(messages=[])
            )
            _ = [event async for event in events]


@pytest.mark.parametrize(
    ("result", "content"),
    [
        (ToolResult(call_id="call_one", output="sunny"), "sunny"),
        (
            ToolResult(call_id="call_one", output="", error="weather service unavailable"),
            "weather service unavailable",
        ),
    ],
)
def test_tool_result_serializes_as_an_openai_tool_message(result: ToolResult, content: str):
    assert serialize_tool_result(result) == {
        "role": "tool",
        "tool_call_id": "call_one",
        "content": content,
    }


def test_assistant_response_serializes_with_stable_tool_call_arguments():
    response = ModelResponse(
        content="Working",
        reasoning="Need two values",
        tool_calls=[
            ToolCall(
                id="call_one",
                name="combine",
                arguments={"z": "最后", "a": 1},
            )
        ],
    )

    assert serialize_assistant_response(response) == {
        "role": "assistant",
        "content": "Working",
        "reasoning_content": "Need two values",
        "tool_calls": [
            {
                "id": "call_one",
                "type": "function",
                "function": {
                    "name": "combine",
                    "arguments": '{"a":1,"z":"最后"}',
                },
            }
        ],
    }
