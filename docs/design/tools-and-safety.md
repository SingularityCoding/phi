# Tools, Environment, and Safety Design

> **Status:** Implemented.

## Authority boundary

The Model proposes a `ToolCall`. Only the Harness may look up the tool, validate arguments, apply
approval policy, execute the handler, enforce timeout, and create the paired `ToolResult`.

The Environment is where observable side effects occur. Tool descriptions and model claims are not
ground truth; file contents, process results, and tests are.

## Tool definition

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    args_schema: Mapping[str, Any]
    args_model: type[BaseModel] | None = None
    approval_class: Literal["read_only", "mutates_workspace", "unconfined"] = "read_only"
    timeout_seconds: float | None = None
    timeout_parameter: str | None = None
```

- `args_schema` always exists and is the sole source for Model-visible tool specifications.
  It is recursively immutable after Tool construction so it cannot drift from local validation.
- Local Python tools derive a Pydantic model from the annotated handler signature and cache its
  JSON Schema.
- MCP tools have a remote JSON Schema but no local Pydantic model; validation remains with the MCP
  server.
- `approval_class` is an approval bucket, not a complete capability inventory and not a confinement
  guarantee.
- `timeout_parameter` optionally identifies a validated, finite positive Model argument that
  supplies a handler-level operation timeout; the dispatcher retains an outer bound with cleanup
  grace.
- Tool handlers may be sync or async. The dispatcher awaits async handlers and adapts sync handlers
  through a worker thread. A timeout bounds the dispatcher's wait, but Python cannot forcibly stop
  an already-running worker thread; side-effectful handlers should therefore be async and preserve
  cancellation semantics.

A `@tool(...)` decorator may construct this definition from a typed Python function. Trusted
injected values such as Environment and Tool Context are not exposed as Model arguments.

## Registry and dispatcher

`ToolRegistry` maps unique names to Tools and provides:

- `register()`;
- `get()`;
- `specs()` for OpenAI-compatible schemas.

The async dispatcher converts every expected processing failure into a `ToolResult`:

- unknown tool name;
- invalid local arguments;
- approval denial;
- handler exception;
- execution timeout.

It uses `asyncio.wait_for()` with the Tool-specific timeout or a dispatcher default. An expected
failure never escapes merely to become `RunStatus.FAILED`.

## Approval policy

```python
class ApprovalPolicy(Protocol):
    async def decide(self, call: ToolCall, tool: Tool) -> Literal["allow", "deny"]: ...
```

`ask` exists only inside a concrete rule set. A policy that needs a human must resolve the question
and return the final allow/deny decision; the dispatcher cannot depend on a UI.

```python
@dataclass(frozen=True)
class ApprovalRule:
    tool_pattern: str
    approval_class: Literal["read_only", "mutates_workspace", "unconfined"] | None
    decision: Literal["allow", "deny", "ask"]

@dataclass(frozen=True)
class ApprovalMode:
    name: str
    rules: tuple[ApprovalRule, ...]
    on_unmatched: Literal["allow", "deny", "ask"]
```

`RuleBasedApprovalPolicy` applies name and class rules, honors denial precedence, defaults safely,
and optionally delegates `ask` to an async resolver.

Preset modes:

| Mode | Read-only | Workspace mutation | Unconfined |
| --- | --- | --- | --- |
| `default` | allow | ask | ask |
| `accept_edits` | allow | allow | ask |
| `plan` | allow | deny | deny |
| `bypass` | allow | allow | allow |

Headless execution uses the same policy with no human resolver and denies all unmatched operations.

“Allow for session” remembers a Tool name in process memory only. It is deliberately broad: after
allowing `bash` for the session, later Bash calls no longer ask. The choice is never persisted and is
cleared at process exit.

## Environment protocol

The Environment exposes async `FileSystem` and `Shell` protocols. Expected operation failures are
returned as result data rather than thrown:

- file error codes include not found, permission denied, not a directory, invalid, and unknown;
- `read_text`, `write_text`, `canonical_path`, and `list_dir` form the v1 file surface;
- `exec` returns a typed process result or execution error.

The implementation must distinguish path resolution for existing paths from safe creation of new
paths. A write target that does not yet exist still requires canonical validation of its existing
parent chain before creation.

## Confinement

`ConfinedEnvironment` has a workspace root and protected patterns such as `.git/**` and `.env*`.
For every `FileSystem` operation it:

1. resolves symbolic links through an appropriate canonical path check;
2. verifies that the resolved path is relative to the workspace root;
3. matches protected patterns against a root-relative path;
4. denies access when any check fails.

This is a structural guarantee only for tools whose handlers use the injected `FileSystem`. It is
not an operating-system sandbox and does not eliminate every concurrent filesystem
time-of-check/time-of-use race.

`bash`, MCP tools, and `spawn_agent` are `unconfined`: their internal effects cannot be enumerated or
path-checked by `ConfinedEnvironment`. They are limited only by cwd, timeout, and approval in v1.
Documentation and UI must state this honestly.

## Built-in tools

| Tool | Arguments | Approval class |
| --- | --- | --- |
| `read` | `path`, optional `offset`, optional `limit` | `read_only` |
| `write` | `path`, `content` | `mutates_workspace` |
| `edit` | `path`, `edits[{old_text,new_text}]` | `mutates_workspace` |
| `bash` | `command`, optional `timeout` | `unconfined` |
| `grep` | `pattern`, path/glob/case/context options, optional `limit` (default `100` matches) | `read_only` |
| `find` | glob `pattern`, optional path, optional `limit` (default `1000` paths) | `read_only` |
| `ls` | optional path, optional `limit` (default `500` entries) | `read_only` |

All seven are registered by default.

`edit` applies multiple replacements against the original file contents. Every `old_text` must be a
unique match and edit ranges must not overlap.

`grep`, `find`, and `ls` use the confined `FileSystem` plus Python matching rather than invoking
shell utilities. This trades large-repository performance for a truthful read-only confinement
guarantee.

An explicit positive `limit` overrides the search default. `grep` counts matching items toward its
limit; context lines rendered around a match do not consume additional match slots.

`bash` defaults to a 120-second timeout unless the model requests an allowed override. Other tools
use the dispatcher default.

## Required tests

- schema generation and local argument validation;
- unknown tool, denial, exception, and timeout as Tool Results;
- sync and async handler adaptation;
- symlink escapes, absolute paths, parent traversal, protected paths, and new-file creation;
- root-relative protected-pattern matching;
- edit uniqueness, overlap, and atomicity;
- built-in search default limits, explicit overrides, `grep` context-line counting, and confinement;
- approval mode matching, denial precedence, headless fail-closed behavior, and session allowance;
- explicit proof that unconfined tools do not receive a false confinement guarantee.

OS sandboxing, native search, and composable capability sets are deferred; see
[`../deferred.md`](../deferred.md).
