# Run, Events, and Hooks Design

> **Status:** Implemented.

## Run model

A Run is one bounded attempt to handle a user request. A Step is one model request and response plus
the Tool Call and Tool Result pairs produced by that response.

```python
class RunStatus(Enum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass(frozen=True)
class Step:
    index: int
    request: ModelRequest
    response: ModelResponse
    tool_results: tuple[ToolResult, ...]

@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    steps: tuple[Step, ...]
    output: str | None = None
    error: Exception | None = None
```

`Step.request` is retained because real Models do not expose the request-recording facility of the
Scripted Model and a Trace must still explain exactly what was sent.

There is no mutable public `Run` object. The operation is an async function returning a `RunResult`;
live progress is represented by Events.

## Loop algorithm

For each Step, up to the Run's maximum:

1. Drain `Hooks.inject_messages` and append returned text as user messages.
2. Emit `ModelCallStarted`.
3. Iterate `model.request_stream()`.
4. Feed every Model Event into `ResponseAssembler` and emit `ModelCallDelta`.
5. Build the normalized response and emit `ModelCallCompleted`.
6. If the response contains Tool Calls, process them sequentially through the dispatcher, recording
   Tool Results, then continue to the next Step.
7. If the response is final, invoke `before_run_complete` when configured.
8. Accept the output or add corrective input and continue under the same Step budget.

Every ordinary Run Step uses streaming internally. `Model.request()` is reserved for one-shot work
that does not need live deltas, such as compaction summarization.

Multiple Tool Calls in one response execute sequentially in v1. The dispatcher boundary remains
async, leaving room for a later explicit concurrency contract.

## Completion and failure semantics

- `COMPLETED` has a final textual output.
- `MAX_STEPS` means the safety budget was exhausted without acceptable completion.
- `FAILED` is reserved for errors the loop cannot safely handle, such as a non-recoverable Model
  error or an internal dispatcher defect.
- `CANCELLED` is produced by the outer service after catching cancellation from the Run task.

Expected tool failures are normal data. Unknown tools, invalid arguments, denial, timeout, and
handler exceptions become `ToolResult.error` and are returned to the Model rather than failing the
Run.

`asyncio.CancelledError` is cleaned up and re-raised inside the loop so asyncio task cancellation
semantics remain intact. The Host-facing service may translate it into a cancelled result after the
task boundary.

## Events

Events are immutable notifications. Every Event carries the Run identifier and a zero-based Event
index; Model and Tool lifecycle Events also carry the zero-based Step index. Mutable Model payloads
are delivered as observation snapshots so a listener cannot change the active Run. Listener return
values never affect behavior.

- `RunStarted(run_id)`
- `ModelCallStarted(step_index, request)`
- `ModelCallDelta(step_index, delta)`
- `ModelCallCompleted(step_index, response, latency_seconds)`
- `ToolCallStarted(call)`
- `ToolCallCompleted(call, result, latency_seconds)`
- `ApprovalDecided(call, decision, mode)`
- `RunFinished(result)`

MCP connection events are defined by the MCP integration but use the same bus.

The Event bus is passed explicitly, never obtained from a global singleton. With no subscribers it
acts as a no-op. Listeners are awaited in subscription order to preserve deterministic observation
and test assertions; they are not fire-and-forget tasks.

The TUI consumes content and reasoning deltas for live rendering. It waits for a fully assembled
Tool Call before rendering a tool card instead of displaying partial JSON fragments.

## Trace

Trace is not a second event mechanism. It is the persisted output of an Event listener, normally
JSONL. It may contain request shape, response shape, latency, approval mode, and failure details for
debugging, but it is not read to resume a Session.

Trace serialization must define explicit wire shapes and redact secrets. Arbitrary Python
exceptions and unbounded raw objects must not be written without normalization.

## Hooks

Hooks are explicit extension points whose results may alter behavior:

```python
class RunDecision(Enum):
    ACCEPT = "accept"
    RETRY = "retry"

@dataclass(frozen=True)
class CompletionDecision:
    decision: RunDecision
    feedback: str | None = None

@dataclass(frozen=True)
class Hooks:
    before_tool_call: ApprovalPolicy | None = None
    before_run_complete: Callable[[RunResult], Awaitable[CompletionDecision]] | None = None
    inject_messages: Callable[[], Awaitable[list[str]]] | None = None
```

- `before_tool_call` is the Approval Policy used before executing a tool.
- `before_run_complete` accepts a provisional result or asks the same loop to continue under the
  same Step budget. `RETRY` requires non-empty corrective feedback, which is appended after the
  provisional Assistant message; `ACCEPT` carries no feedback.
- `inject_messages` drains non-destructive steering messages at Step boundaries. It does not cancel
  or restart the Run.

Additional generic hooks are not created until a real capability needs them.

## Required tests

- normal completion with and without tools;
- exact request history for a multi-Step Tool round trip;
- maximum-Step termination;
- expected tool errors remaining inside the loop;
- fatal Model errors producing `FAILED`;
- cancellation propagation and cleanup;
- exact Event order, including streaming deltas;
- listener ordering and no-op bus behavior;
- completion retry retaining the same Step budget;
- injected messages appearing at the next Step boundary;
- Scripted Model exhaustion exposing accidental extra calls.

Selective parallel tool execution is deferred; see [`../deferred.md`](../deferred.md).
