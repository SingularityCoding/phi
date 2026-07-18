from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from phi.model.assembler import ResponseAssembler
from phi.model.errors import (
    ModelContextLimitError,
    ModelHTTPError,
    ModelProtocolError,
    ModelTimeoutError,
)
from phi.model.events import (
    ContentDelta,
    FinishEvent,
    ModelEvent,
    ReasoningDelta,
    ToolCallDelta,
    UsageEvent,
)
from phi.model.types import ModelConfig, ModelRequest, ModelResponse, ToolResult, Usage


class OpenAICompatibleModel:
    """OpenAI-compatible HTTP and SSE adapter for one stateless Model request."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None

    async def __aenter__(self) -> OpenAICompatibleModel:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally-created HTTP client, if this Model owns it."""

        if self._owns_client:
            await self._client.aclose()

    async def request(self, request: ModelRequest) -> ModelResponse:
        assembler = ResponseAssembler()
        async for event in self.request_stream(request, _transport_stream=False):
            assembler.absorb(event)
        return assembler.build()

    async def request_stream(
        self,
        request: ModelRequest,
        *,
        _transport_stream: bool = True,
    ) -> AsyncIterator[ModelEvent]:
        async for event in self._request_events(request, stream=_transport_stream):
            yield event

    async def _request_events(
        self,
        request: ModelRequest,
        *,
        stream: bool,
    ) -> AsyncIterator[ModelEvent]:
        if stream:
            async for event in self._stream_events(request):
                yield event
            return

        payload = self._serialize_request(request, stream=False)
        try:
            response = await self._client.post(
                self._chat_completions_url,
                json=payload,
                headers=self._headers,
                timeout=self._config.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError from exc
        except httpx.RequestError as exc:
            raise ModelHTTPError(status_code=0, body=str(exc)) from exc

        if not response.is_success:
            raise _http_error(response.status_code, response.text)

        try:
            raw: object = response.json()
        except ValueError as exc:
            raise ModelProtocolError("Model response body is not valid JSON") from exc
        for event in _events_from_non_streaming_response(raw):
            yield event

    async def _stream_events(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        payload = self._serialize_request(request, stream=True)
        saw_done = False
        saw_finish_or_usage = False
        seen_tool_call_indices: set[int] = set()
        try:
            async with self._client.stream(
                "POST",
                self._chat_completions_url,
                json=payload,
                headers=self._headers,
                timeout=self._config.request_timeout_seconds,
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise _http_error(response.status_code, response.text)

                async for data in _iter_sse_data(response):
                    if data == "[DONE]":
                        saw_done = True
                        break
                    try:
                        raw: object = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ModelProtocolError("SSE data is not valid JSON") from exc
                    for event in _events_from_stream_chunk(raw):
                        if isinstance(event, (FinishEvent, UsageEvent)):
                            saw_finish_or_usage = True
                        if isinstance(event, ToolCallDelta):
                            if event.index in seen_tool_call_indices:
                                event = ToolCallDelta(
                                    index=event.index,
                                    arguments_fragment=event.arguments_fragment,
                                )
                            else:
                                seen_tool_call_indices.add(event.index)
                        yield event
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError from exc
        except httpx.RequestError as exc:
            raise ModelHTTPError(status_code=0, body=str(exc)) from exc

        if not saw_done:
            raise ModelProtocolError("Model stream ended before the [DONE] sentinel")
        if not saw_finish_or_usage:
            raise ModelProtocolError("Model stream ended without a finish or usage event")

    def _serialize_request(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model or self._config.default_model,
            "messages": request.messages,
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    @property
    def _chat_completions_url(self) -> str:
        return f"{self._config.base_url.rstrip('/')}/chat/completions"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.api_key.get_secret_value()}"}


def _http_error(status_code: int, body: str) -> ModelHTTPError:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ModelHTTPError(status_code=status_code, body=body)
    if isinstance(payload, dict):
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            context_limit_codes = {
                "context_length_exceeded",
                "context_window_exceeded",
                "max_context_length_exceeded",
            }
            structured_values = (raw_error.get("code"), raw_error.get("type"))
            if any(
                isinstance(value, str) and value in context_limit_codes
                for value in structured_values
            ):
                return ModelContextLimitError(status_code=status_code, body=body)
    return ModelHTTPError(status_code=status_code, body=body)


def serialize_tool_result(result: ToolResult) -> dict[str, str]:
    """Serialize a Harness-produced Tool Result as an OpenAI-compatible message."""

    return {
        "role": "tool",
        "tool_call_id": result.call_id,
        "content": result.error if result.error is not None else result.output,
    }


def serialize_assistant_response(response: ModelResponse) -> dict[str, Any]:
    """Serialize one normalized Assistant response for a later Model request."""

    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.content,
    }
    if response.reasoning is not None:
        message["reasoning_content"] = response.reasoning
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            }
            for call in response.tool_calls
        ]
    return message


def _events_from_non_streaming_response(raw_value: object) -> list[ModelEvent]:
    raw = _object(raw_value, "Model response")
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelProtocolError("Model response choices must be a non-empty list")

    choice = _object(choices[0], "Model response choice")
    message = _object(choice.get("message"), "Model response message")
    events: list[ModelEvent] = []

    reasoning = _optional_text(message.get("reasoning_content"), "reasoning_content")
    if reasoning is not None:
        events.append(ReasoningDelta(reasoning))

    content = _optional_text(message.get("content"), "content")
    if content is not None:
        events.append(ContentDelta(content))

    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            raise ModelProtocolError("Model response tool_calls must be a list or null")
        events.extend(_tool_call_events(tool_calls))

    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ModelProtocolError("Model response finish_reason must be a string or null")
    events.append(FinishEvent(finish_reason=finish_reason, raw=raw))

    usage = _normalize_usage(raw.get("usage"))
    if usage is not None:
        events.append(UsageEvent(usage=usage, raw=raw))
    return events


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":") or not line.startswith("data:"):
            continue
        data = line.removeprefix("data:")
        if data.startswith(" "):
            data = data[1:]
        data_lines.append(data)

    if data_lines:
        yield "\n".join(data_lines)


def _events_from_stream_chunk(raw_value: object) -> list[ModelEvent]:
    raw = _object(raw_value, "Model stream chunk")
    choices = raw.get("choices")
    if not isinstance(choices, list):
        raise ModelProtocolError("Model stream chunk choices must be a list")

    usage = _normalize_usage(raw.get("usage"))
    if not choices:
        if usage is None:
            raise ModelProtocolError("Model stream chunk choices cannot be empty without usage")
        return [UsageEvent(usage=usage, raw=raw)]

    choice = _object(choices[0], "Model stream chunk choice")
    delta = _object(choice.get("delta"), "Model stream chunk delta")
    events: list[ModelEvent] = []

    reasoning = _optional_text(delta.get("reasoning_content"), "reasoning_content")
    if reasoning is not None:
        events.append(ReasoningDelta(reasoning))

    content = _optional_text(delta.get("content"), "content")
    if content is not None:
        events.append(ContentDelta(content))

    tool_calls = delta.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            raise ModelProtocolError("Model stream chunk tool_calls must be a list or null")
        events.extend(_stream_tool_call_events(tool_calls))

    finish_reason = choice.get("finish_reason")
    if finish_reason is not None:
        if not isinstance(finish_reason, str):
            raise ModelProtocolError("Model stream finish_reason must be a string or null")
        events.append(FinishEvent(finish_reason=finish_reason, raw=raw))

    if usage is not None:
        events.append(UsageEvent(usage=usage, raw=raw))
    return events


def _tool_call_events(raw_tool_calls: list[object]) -> list[ToolCallDelta]:
    events: list[ToolCallDelta] = []
    for index, raw_tool_call in enumerate(raw_tool_calls):
        tool_call = _object(raw_tool_call, f"Tool Call at index {index}")
        call_id = tool_call.get("id")
        if not isinstance(call_id, str):
            raise ModelProtocolError(f"Tool Call at index {index} id must be a string")
        function = _object(tool_call.get("function"), f"Tool Call at index {index} function")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str):
            raise ModelProtocolError(f"Tool Call at index {index} name must be a string")
        if not isinstance(arguments, str):
            raise ModelProtocolError(f"Tool Call at index {index} arguments must be a string")
        events.append(
            ToolCallDelta(
                index=index,
                id=call_id,
                name=name,
                arguments_fragment=arguments,
            )
        )
    return events


def _stream_tool_call_events(raw_tool_calls: list[object]) -> list[ToolCallDelta]:
    events: list[ToolCallDelta] = []
    for position, raw_tool_call in enumerate(raw_tool_calls):
        tool_call = _object(raw_tool_call, f"Stream Tool Call at position {position}")
        index = tool_call.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ModelProtocolError(
                f"Stream Tool Call at position {position} index must be a non-negative integer"
            )

        call_id = tool_call.get("id")
        if call_id is not None and not isinstance(call_id, str):
            raise ModelProtocolError(
                f"Stream Tool Call at index {index} id must be a string or null"
            )

        name: str | None = None
        arguments = ""
        raw_function = tool_call.get("function")
        if raw_function is not None:
            function = _object(raw_function, f"Stream Tool Call at index {index} function")
            raw_name = function.get("name")
            if raw_name is not None and not isinstance(raw_name, str):
                raise ModelProtocolError(
                    f"Stream Tool Call at index {index} name must be a string or null"
                )
            name = raw_name
            raw_arguments = function.get("arguments")
            if raw_arguments is not None and not isinstance(raw_arguments, str):
                raise ModelProtocolError(
                    f"Stream Tool Call at index {index} arguments must be a string or null"
                )
            if raw_arguments is not None:
                arguments = raw_arguments

        events.append(
            ToolCallDelta(
                index=index,
                id=call_id,
                name=name,
                arguments_fragment=arguments,
            )
        )
    return events


def _normalize_usage(raw_usage: object) -> Usage | None:
    if raw_usage is None:
        return None
    usage = _object(raw_usage, "Model response usage")
    return Usage(
        prompt_tokens=_non_negative_int(usage.get("prompt_tokens"), "usage.prompt_tokens"),
        completion_tokens=_non_negative_int(
            usage.get("completion_tokens"), "usage.completion_tokens"
        ),
        total_tokens=_non_negative_int(usage.get("total_tokens"), "usage.total_tokens"),
        cached_tokens=_usage_detail_count(
            usage,
            detail_field="prompt_tokens_details",
            count_field="cached_tokens",
        ),
        reasoning_tokens=_usage_detail_count(
            usage,
            detail_field="completion_tokens_details",
            count_field="reasoning_tokens",
        ),
    )


def _usage_detail_count(
    usage: dict[str, Any],
    *,
    detail_field: str,
    count_field: str,
) -> int | None:
    raw_details = usage.get(detail_field)
    if raw_details is None:
        return None
    details = _object(raw_details, f"usage.{detail_field}")
    raw_count = details.get(count_field)
    if raw_count is None:
        return None
    return _non_negative_int(raw_count, f"usage.{detail_field}.{count_field}")


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelProtocolError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelProtocolError(f"Model response {field} must be a string or null")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelProtocolError(f"{field} must be a non-negative integer")
    return value
