# Sessions and Context Design

> **Status:** Design and core application-service implementation complete; Host integration is a
> later roadmap stage.

## Concepts

Phi does not maintain separate canonical objects called State and Transcript.

- **Entries** are durable nodes in a conversation tree.
- A **Conversation View** is materialized by walking one branch of Entries and applying effective
  settings.
- **Context** is the finite projection sent to a Model.
- **Trace** is a separate observability record and is never used to resume a Session.

## Entry tree

Entries have an ID, optional parent ID, and timestamp. Phi v1 defines four variants:

```python
class UserMessageEntry(EntryBase):
    content: str

class AssistantMessageEntry(EntryBase):
    content: str
    reasoning: str | None
    tool_calls: list[ToolCall]

class ToolResultEntry(EntryBase):
    result: ToolResult

class CompactionEntry(EntryBase):
    summary: str
    tokens_before: int
    tokens_before_source: Literal["provider", "estimate"]
    first_kept_entry_id: str
```

The persistence granularity is one message or structural entry, not an entire Run or Step. This
allows a fork to start at a precise point in history.

Persisted entries use Pydantic because disk data is untrusted: it may come from an older version,
manual edits, or an interrupted write. Before the format stabilizes, the implementation must choose
an explicit variant discriminator and schema-version policy rather than relying on shape guessing.

## Session files

Each Session has separate durable products:

- `<session_id>.jsonl` stores Entries required to reconstruct a conversation;
- `<session_id>.trace.jsonl` stores serialized Events for debugging;
- Session metadata records identity, lineage, current leaf, display name, and model.

Trace data is never read as conversation state.

```python
class SessionMetadata(BaseModel):
    id: str
    created_at: datetime
    leaf_id: str | None
    parent_session_id: str | None
    fork_point_entry_id: str | None
    name: str | None = None
    model: str | None = None
    origin: Literal["new", "fork", "subagent"] | None = None
```

Session files live under a configurable directory, defaulting to `~/.phi/sessions/`, rather than in
the working repository. Conversation contents and local paths must not be committed accidentally.

Writes must define crash behavior before implementation is considered durable: incomplete final
JSONL records need a clear error or recovery policy, and metadata updates must not silently point to
an Entry that was never persisted.

## Resume and fork

`path_to_root(leaf_id)` walks parent IDs to materialize the selected branch.

A user fork creates a new Session file that references the parent Session and exact fork point
instead of copying the common prefix. When materializing that fork, traversal crosses into the
parent Session at `fork_point_entry_id`.

A Subagent Session also records `parent_session_id`, but has no fork point and does not inherit the
parent conversation. Its parent link records delegation lineage only; its Context begins with the
delegated task.

## Session services

```python
@dataclass(frozen=True)
class PromptBudgetAnchor:
    model_id: str
    request: ModelRequest
    local_estimate: int
    prompt_tokens: int

@dataclass(frozen=True)
class SessionHandle:
    session_id: str
    leaf_id: str | None
    session_file: Path
    metadata: SessionMetadata
    prompt_budget_anchor: PromptBudgetAnchor | None = None

async def create_session(...) -> SessionHandle: ...
async def resume_session(..., session_id: str) -> SessionHandle: ...
async def fork_session(..., session_id: str, entry_id: str) -> SessionHandle: ...
async def list_sessions(...) -> list[SessionMetadata]: ...

async def send_message(
    handle: SessionHandle,
    text: str,
    *,
    settings: Settings,
    model: Model,
    model_info: ModelInfo | None,
    tools: ToolRegistry,
    hooks: Hooks | None = None,
    events: EventBus | None = None,
) -> tuple[SessionHandle, RunResult]: ...
```

Services are stateless functions. The caller owns the current immutable handle. The optional prompt
budget anchor is runtime-only state carried by that handle; it is not written to Session metadata or
Entries. A successful Run may populate it from the final Step's exact request and
`Usage.prompt_tokens`. Resume, fork, model change, compaction, and branch switching clear it; missing
Usage leaves it as `None`.

The anchor stores the resolved, non-null model ID and a deep snapshot of the previous request. It
must not alias mutable message or Tool-schema lists owned by a live Context builder.

`send_message()`:

1. materializes the selected Conversation View;
2. appends the pending user message;
3. builds Context;
4. performs threshold compaction when needed;
5. calls the Harness Run;
6. performs at most one overflow-compaction retry when the failure is a recognized context-limit
   error;
7. splits completed Steps into durable Entries;
8. returns an updated handle.

An optional future stateful runtime can wrap these functions without moving their behavior.

To preserve dependency direction, the Harness Context builder accepts an already materialized
Conversation View rather than a `SessionHandle`. Likewise, Harness compaction code owns pure policy;
the Session service owns Entry traversal, Model-call orchestration, and persistence of the resulting
`CompactionEntry`.

## Context

```python
@dataclass(frozen=True)
class Context:
    system_prompt: str
    tools: list[dict]
    messages: list[dict]
    dropped_summary: str | None = None
```

- `system_prompt` contains stable instructions assembled for every request.
- `tools` contains complete Tool Registry schemas.
- `messages` contains the selected budgeted conversation in wire form.
- `dropped_summary` explains omitted history when applicable.

The Context inspector renders complete content and character counts. Phi does not claim exact
per-section token counts without a tokenizer that matches the selected model. A provider-reported
Usage total may be shown as aggregate information, while a local approximation must be labelled as
an estimate rather than Usage.

## Project instructions

```python
@dataclass(frozen=True)
class ProjectInstructions:
    content: str
    source_path: Path | None
```

Phi loads a root `AGENTS.md`, falling back to root `CLAUDE.md`. v1 does not implement nested or
path-scoped instruction discovery. Global personal instructions and project instructions are
concatenated because prose has no natural key for last-write-wins replacement.

The system prompt order is:

1. Phi base instructions;
2. project instructions;
3. the Model-invocable Skill menu.

Instructions are reloaded or re-injected as stable prompt input and are never summarized by
compaction.

## Compaction

```python
@dataclass(frozen=True)
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    summary_max_tokens: int = 4_096
    max_input_tokens: int | None = None
```

Environment-backed `Settings` uses flat fields and bootstrap converts them into the trusted value
above:

| Environment variable | `CompactionSettings` field | Default |
| --- | --- | --- |
| `PHI_COMPACTION_ENABLED` | `enabled` | `true` |
| `PHI_COMPACTION_RESERVE_TOKENS` | `reserve_tokens` | `16384` |
| `PHI_COMPACTION_KEEP_RECENT_TOKENS` | `keep_recent_tokens` | `20000` |
| `PHI_COMPACTION_SUMMARY_MAX_TOKENS` | `summary_max_tokens` | `4096` |
| `PHI_COMPACTION_MAX_INPUT_TOKENS` | `max_input_tokens` | unset |

`reserve_tokens` and `keep_recent_tokens` must be non-negative; `summary_max_tokens` and a supplied
`max_input_tokens` must be positive. `enabled=False` disables manual, threshold, and overflow
compaction; `/compact` reports that compaction is disabled rather than silently doing nothing.

Compaction has three trigger reasons:

- `manual` from `/compact [focus]`;
- `threshold` before a request exceeds the safe Context window;
- `overflow` after one recognized provider context-limit error.

The process is:

1. deterministically decide whether compaction is required;
2. walk backward to retain the configured recent budget;
3. choose a cut only at a message boundary, never between an Assistant Tool Call and its Tool
   Result;
4. issue a separate non-streaming Model request whose only job is structured summarization;
5. append a `CompactionEntry` without deleting old Entries;
6. materialize future views from the summary plus retained recent history.

Threshold Context budgeting is prospective and, when an effective input limit is known, runs before
a Model request; Usage is retrospective and may be absent. Phi does not make a separate remote
preflight token-count request. Such an endpoint is outside the OpenAI-compatible Model boundary and
may use a tokenizer or request shape that does not match the eventual Model call.

### Local estimate

Phi estimates the final Model-visible request after Context has been converted into
`ModelRequest.messages` and `ModelRequest.tools`. It serializes this object as compact, stable-key
JSON with `ensure_ascii=False`:

```python
{"messages": request.messages, "tools": request.tools}
```

For that canonical JSON string, the v1 policy is:

```text
E(request) = ceil(ascii_codepoints / 4)
           + non_ascii_codepoints
           + 4 * message_count
           + 8 * tool_count
           + 16
```

This deliberately treats each non-ASCII code point as one estimated token instead of applying a
plain character-quartering rule that would undercount Chinese text. The fixed terms conservatively
represent message, tool, and outer request framing. These coefficients are Phi budgeting policy,
not claims about a provider tokenizer.

When a `PromptBudgetAnchor` describes the exact previous request to the same model,
`Usage.prompt_tokens` is the preferred anchor. It is usable only when the system prompt, Tool
schemas, and dropped summary are unchanged and the previous messages are a structural prefix of the
candidate messages. Given those conditions:

```text
anchored = prompt_tokens + max(0, E(candidate) - E(previous))
estimated_prompt_tokens = max(E(candidate), anchored)
```

Any anchored value with a local increment is still an estimate. Phi does not use
`Usage.total_tokens` as Context size because completion and reasoning tokens are not necessarily
reinserted into the next request exactly as generated.

Phi estimates the complete model-visible Context when no usable anchor exists, including:

- the first Model request;
- a resumed Session whose last Usage was not persisted;
- a response whose Usage was absent;
- a model change;
- a Context rebuilt by compaction or another non-append transformation.

Because estimation operates on the final request, the full estimate includes the system prompt,
full Tool schemas, messages, Tool Call arguments, Tool Results, dropped summary, and wire/role
overhead. Cached tokens remain part of prompt size even though they may be cheaper to process.

Every persisted budget value records whether it is provider-reported or estimated. An estimate
never becomes `ModelResponse.usage`.

### Threshold and input limit

The effective input limit is the smaller of `ModelInfo.max_input_tokens` and configured
`CompactionSettings.max_input_tokens` when both exist, or whichever one exists. The configured value
is a safety cap or fallback; it never enlarges a provider-reported limit. Bootstrap supplies the
selected model's `ModelInfo` to the Session service rather than making the stateless Model adapter
own Context policy.

When neither value exists, Phi does not invent a default Context window. It emits a diagnostic and
skips proactive threshold compaction; manual compaction and the bounded overflow path remain
available in an explicitly best-effort compatibility mode, without a pre-request fit guarantee.
When an effective limit exists:

```text
safe_prompt_limit = effective_max_input_tokens - reserve_tokens
should_compact = enabled and estimated_prompt_tokens > safe_prompt_limit
```

Equality does not trigger compaction. A non-positive `safe_prompt_limit` is a configuration/capacity
error. Threshold compaction runs at most once for one `send_message()` call, then Phi rebuilds and
re-estimates the request. If stable instructions, Tool schemas, or mandatory recent history still
exceed the safe limit, Phi returns a Context budget error instead of repeatedly compacting.

### Cut selection and summary

Cut selection treats an Assistant message containing Tool Calls and all of their Tool Results as one
atomic unit. The newest complete atomic unit is always retained; a pending User message, when
present, belongs to that mandatory suffix. Empty history, or history with no older unit to drop,
returns `NothingToCompact`. This guarantees that `CompactionEntry.first_kept_entry_id` remains
defined even when `keep_recent_tokens=0`.

When `ModelInfo.max_output_tokens` is available, the effective summary output limit is the smaller
of it and `summary_max_tokens`; otherwise it is `summary_max_tokens`. Phi uses one executable
candidate-building algorithm rather than adding independent Entry estimates:

1. Build the exact post-compaction request shape with stable Context, an empty summary-message
   envelope, and a candidate retained suffix.
2. Compute `recent_size` as the difference between `E()` for that candidate and `E()` for the same
   request with no retained suffix.
3. When a safe prompt limit is known, a suffix fits only when `E(candidate)` plus the effective
   summary output limit does not exceed `safe_prompt_limit`.
4. Start with the mandatory newest unit and add older complete units while `recent_size` is below
   `keep_recent_tokens` and the resulting candidate still fits. Capacity wins over the target.

Thus `keep_recent_tokens` is a target, not an unconditional guarantee. If the mandatory suffix does
not fit, compaction returns a Context capacity error rather than making an invalid cut.

When no effective input limit is known, manual and overflow compaction use
`keep_recent_tokens` as the cut target without claiming that the result fits a particular window;
the summary request or one permitted retry may still receive a provider context-limit error.

When compacting a view that already has `dropped_summary`, the old summary is the oldest input to
the new summary request, followed by the newly dropped Entries. The new `CompactionEntry.summary`
must cover both; summarizing only the newly dropped Entries would lose information from the earlier
compaction.

The summary request is non-streaming, exposes no Tools, and sets `max_tokens` to the effective
summary output limit. For manual `/compact [focus]`, `focus` is inserted only as a user-supplied
emphasis in the summary prompt; it does not change deterministic cut selection or any budget.
Summary input is budget-checked when an effective limit is known. Success requires non-empty textual
content and no Tool Calls. An invalid response, an oversized summary input, or a rebuilt Context
that still exceeds the safe limit fails explicitly; summary generation does not recursively compact
itself.

### Overflow recovery

Overflow recovery applies only to a recognized provider context-limit error and performs at most one
compaction plus one retry. v1 retries only when the failed attempt completed no Tool Calls. Once any
Tool Call has completed, restarting the Run could repeat an externally observable action; without a
persisted mid-Run resume boundary, Phi returns the failure instead.

## Model selection

The selected model is a branch property stored in Session metadata, not a `ModelChangeEntry` in the
middle of history.

Model identity participates in provider prompt-cache eligibility. A prefix cached for one model
cannot be reused by another, so the first request on a different model must process its Context
under a new cache identity. Forking does not avoid that provider-level cost; it preserves the old
branch and keeps each branch model-stable instead of mutating model identity midway through its
history.

- Before a branch has produced an `AssistantMessageEntry`, there is no completed Model-output prefix
  to preserve, so selecting a model updates its metadata in place.
- After the branch has produced model output, selecting another model implicitly forks at the
  current leaf and assigns the new model to the new branch.
- Subagents may select a different model when their isolated Session is created.

Available choices come from `list_available_models()` rather than a duplicated Host-side list.

## Required tests

- Entry validation, serialization, version handling, and interrupted-write behavior;
- path materialization for roots, branches, forks, compaction, and corrupt parent references;
- Subagent lineage without inherited parent Context;
- Session/Trace separation;
- create, resume, list, rename, fork, and leaf switching;
- Context system prompt, tools, messages, and dropped summary;
- Tool Call and Tool Result atomicity across compaction cuts;
- manual, threshold, overflow, and single-retry compaction;
- `PHI_COMPACTION_*` parsing, validation, defaults, and disabled behavior;
- exact local-estimate policy for ASCII, non-ASCII, messages, Tool schemas, calls, results, and
  framing;
- `Usage.prompt_tokens` anchoring, estimation of subsequent additions, and complete-estimate
  fallback for first requests, resumed Sessions, missing Usage, model changes, and compaction;
- prompt-anchor invalidation after non-append transformations and persistence of estimate source;
- effective input-limit precedence, missing-limit diagnostics, and irreducible budget failures;
- recent-history targets, mandatory atomic units, summary limits, and no recursive compaction;
- repeated compaction preserving the previous dropped summary;
- immutable prompt-anchor snapshots and summary responses rejecting Tool Calls;
- overflow recovery refusal after any completed Tool Call;
- model selection before output, implicit fork after output, and preservation of the original
  branch model;
- session directory isolation from the working repository.
