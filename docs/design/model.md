# Model Gateway Design

> **Status:** Design complete; implementation not started.

## Boundary

`phi.model` represents one stateless model request. It owns:

- OpenAI-compatible HTTP and SSE transport;
- request serialization;
- normalization of content, reasoning, Tool Calls, usage, and finish reasons;
- conversion of network and protocol failures into explicit Model errors;
- deterministic Scripted Model behavior for tests.

It does not own conversation history, system instructions, Context construction, tool execution,
approval, retries chosen by the Harness, Sessions, compaction, or UI rendering.

Phi calls the centrally deployed LiteLLM Proxy through OpenAI-compatible HTTP. It does not use the
LiteLLM Python SDK because provider routing and normalization already occur at the Proxy.

## Public protocol

```python
class Model(Protocol):
    async def request(self, request: ModelRequest) -> ModelResponse: ...
    def request_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

Two implementations are required:

- `OpenAICompatibleModel` for the real Proxy;
- `ScriptedModel` for deterministic offline tests.

The Harness holds a Model for a Run but the Model does not retain conversation state.

## Trusted in-memory values

Model-domain values use standard-library dataclasses, generally frozen where mutation is not part
of their job.

### `ModelConfig`

- `base_url: str`
- `api_key: SecretStr`
- `default_model: str`
- `request_timeout_seconds: float`

`ModelConfig` is not `Settings`. Settings parses untrusted environment strings; trusted bootstrap
code converts it into the Model-specific value so `phi.model` does not import `phi.settings`.

### `ModelRequest`

- `messages: list[dict]`
- `tools: list[dict]`
- `model: str | None`
- `temperature: float | None`
- `max_tokens: int | None`

Messages and tool schemas remain wire-shaped dictionaries at this adapter boundary.

### `ModelResponse`

- `content: str | None`
- `reasoning: str | None`
- `tool_calls: list[ToolCall]`
- `usage: Usage | None`
- `finish_reason: str | None`
- `raw: dict`

`finish_reason` remains an open string because proxies may introduce values Phi has not enumerated.

### `ToolCall` and `ToolResult`

`ToolCall` contains `id`, `name`, and parsed `arguments: dict[str, Any]`. Invalid JSON is rejected at
the Model boundary instead of producing a partially valid Tool Call.

`ToolResult` contains `call_id`, `output`, and an optional error string. The Model adapter serializes
it for a later request; the Harness remains the only component allowed to create it through tool
processing.

### `Usage`

- `prompt_tokens: int`
- `completion_tokens: int`
- `total_tokens: int`
- `cached_tokens: int | None`
- `reasoning_tokens: int | None`

`cached_tokens` is normalized from `prompt_tokens_details.cached_tokens`, and `reasoning_tokens` is
normalized from `completion_tokens_details.reasoning_tokens`, when supplied. Cached tokens are a
subset of prompt tokens rather than an additional amount. Provider-specific fields remain available
in `ModelResponse.raw` but do not expand the normalized type without a separate design decision.

Usage is provider-reported observation data, not a prerequisite for a valid Model response. An
absent or null wire `usage` becomes `ModelResponse.usage = None`; the adapter does not invent Usage
from a local token estimate. If a wire `usage` object is present, its three required totals must be
non-negative integers. A malformed supplied object is a `ModelProtocolError` rather than silently
becoming `None`.

Pre-request token counting is not part of the `Model` protocol. In particular, `phi.model` does not
call LiteLLM-specific token-counting endpoints. Prospective Context budgeting and its local fallback
estimate belong to the Harness; only counts returned with a completed Model request become `Usage`.

## Error hierarchy

- `ModelError` — base class;
- `ModelHTTPError(status_code, body)` — non-success HTTP response;
- `ModelProtocolError(detail)` — malformed success response, such as empty choices, null message,
  or invalid Tool Call JSON;
- `ModelTimeoutError` — request timeout.

Errors must retain enough typed information for the Harness or Session service to choose policy
without matching exception strings. In particular, overflow compaction may inspect an HTTP error to
recognize a provider-specific context-limit signal.

## Streaming events

```python
@dataclass(frozen=True)
class ContentDelta:
    text: str

@dataclass(frozen=True)
class ReasoningDelta:
    text: str

@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""

@dataclass(frozen=True)
class FinishEvent:
    finish_reason: str | None
    raw: dict

@dataclass(frozen=True)
class UsageEvent:
    usage: Usage
    raw: dict
```

`ToolCallDelta.index` groups fragments belonging to the same Tool Call. The ID and name may appear
only in the first fragment; JSON arguments are parsed only after all fragments are assembled.

`FinishEvent` and `UsageEvent` are independent because an OpenAI-compatible stream may report its
finish reason and Usage in different chunks. `OpenAICompatibleModel` requests
`stream_options={"include_usage": true}` for ordinary streaming calls, but the resulting Usage
remains optional because a compatible Proxy or provider may still omit it.

## Response assembly

`ResponseAssembler` is a mutable accumulator in `phi.model`:

- `absorb(event)` updates accumulated content, reasoning, tool fragments, usage, finish reason, and
  final raw data;
- `build()` returns one normalized `ModelResponse`;
- the streaming adapter consumes through the `[DONE]` sentinel and does not stop when it first sees
  a finish reason;
- a trailing Usage chunk with an empty delta is absorbed without creating content or replacing the
  previously observed finish reason;
- malformed assembled Tool Call arguments raise the existing protocol error;
- for streaming, `ModelResponse.raw` contains the final event's raw chunk rather than an invented
  concatenation of all chunks.

The accumulator must be incremental so the Harness can forward every delta as an Agent Event before
the final response exists. Non-streaming and streaming paths must produce the same normalized final
shape.

An empty content string is not by itself a protocol error. A reasoning model may consume the entire
completion budget as reasoning and return empty visible content with `finish_reason="length"`; a
Tool Call response may also have no textual content. The Harness decides whether such a normalized
response can advance the Run.

## Verified Proxy observations

The following behavior was verified against the course LiteLLM Proxy and its configured
`deepseek-v4-flash` model on 2026-07-17. These are contract observations that motivate the adapter
rules above, not fixed token totals that tests should hard-code:

- non-streaming text and Tool Call responses included Usage;
- plain streaming omitted Usage, while `stream_options.include_usage=true` produced one Usage
  chunk after the finish chunk and before `[DONE]`;
- that trailing chunk contained one choice with an empty delta and no finish reason rather than an
  empty `choices` list;
- reasoning arrived as `reasoning_content`, and its tokens were included in `completion_tokens`;
- `max_tokens=1` produced empty visible content with `finish_reason="length"` because the single
  completion token was a reasoning token;
- non-streaming responses exposed cache information both through
  `prompt_tokens_details.cached_tokens` and additional Proxy-specific cache fields.
- the LiteLLM-specific `/utils/token_counter` endpoint used an `openai_tokenizer` count for the
  configured model; that count differed from the completed request's `prompt_tokens` and did not
  change when a large Tool schema was supplied, so Phi does not use this endpoint for Context
  budgeting.

## Scripted Model

The Scripted Model:

- consumes a supplied response or event script in order;
- records every `ModelRequest` it receives;
- raises loudly when the script is exhausted;
- never repeats the final scripted response implicitly;
- performs no network access.

`request()` may internally consume `request_stream()` through `ResponseAssembler` so script
advancement and exhaustion semantics have one implementation.

## Model discovery

```python
@dataclass(frozen=True)
class ModelInfo:
    id: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
```

`list_available_models()` calls the Proxy's `/v1/models` endpoint. The Proxy is expected to filter
models according to the virtual key. Phi ignores untrustworthy placeholder metadata such as generic
`created` and `owned_by` values.

## Required tests

- successful non-streaming response normalization;
- missing or null Usage accepted as `None`, but malformed supplied Usage rejected;
- cached and reasoning token-detail normalization without double-counting cached tokens;
- streaming content, reasoning, a separate trailing Usage event, and multiple fragmented Tool
  Calls;
- request serialization includes `stream_options.include_usage=true` for streaming calls;
- streaming assembly continues past a finish event through `[DONE]` and absorbs an empty-delta
  Usage chunk;
- equivalence of streaming and non-streaming final response shape;
- empty visible content with a length finish reason or Tool Calls remains a normalized response;
- invalid Tool Call JSON;
- empty or malformed choices;
- HTTP, authentication, timeout, and unknown finish-reason behavior;
- Scripted Model request recording and exhaustion;
- opt-in real Proxy contracts for basic, streaming, constrained-budget, and Tool Call responses;
  assert protocol shape rather than exact live token totals.

Per-call cost display is outside v1; see [`../deferred.md`](../deferred.md).
