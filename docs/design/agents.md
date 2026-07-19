# Multi-agent Delegation Design

> **Status:** Implemented.

## Scope

Phi v1 implements parent-to-child Delegation, not Peer or Team collaboration. A parent Agent may
spawn an isolated Subagent, inspect its status, provide non-destructive steering, and close it. The
child uses the same Session, Run, Tool, Event, and Hook primitives as the parent; there is no second
agent loop.

The isolated Context is a primary feature. A Subagent receives its delegated task and selected
definition, not the parent's conversation history. Its own Session and Trace preserve the complete
child interaction for later inspection.

## Agent definitions

```python
@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None = None
    model: str | None = None
    disable_model_invocation: bool = False
```

Definitions use YAML frontmatter plus Markdown instructions and reuse the Skill discovery policy:

- global definitions under `~/.phi/agents/`;
- project definitions under `.phi/agents/`;
- project definitions override global definitions of the same name;
- malformed definitions warn and are skipped without blocking valid definitions.

`disable_model_invocation=True` omits a definition from the Model-visible definition catalog. The
`spawn_agent` Tool must also reject an exact `agent_type` naming that definition before it creates a
Session, task, or registry entry. The definition remains loaded and addressable inside the
definition registry, but v1 Hosts do not expose a user-facing Agent Definition selector; this field
does not imply that such a user route already exists.

When no type is selected, `DEFAULT_AGENT_DEFINITION` supplies a general-purpose isolated child that
inherits the parent's available tools. Explicit `tools` restrict the child registry. An explicit
spawn-time model overrides the definition model, which overrides the parent default.

Fields for private Memory, worktree isolation, custom hooks, permission modes, embedded MCP
servers, and other advanced runtimes are not added before Phi has those capabilities. Long-term
Memory is deferred in [`../deferred.md`](../deferred.md).

## Model-visible tools

Bootstrap renders the name and description of every Model-enabled definition into `spawn_agent`'s
Model-visible Tool description. That catalog supplies the choices and guidance for
`spawn_agent.agent_type`; disabled definitions are never rendered into it.

```text
spawn_agent(task, agent_type?, model?) -> agent_id
check_agent(agent_id, timeout_seconds?) -> status/result
steer_agent(agent_id, message) -> acknowledgement
list_agents() -> child summaries
close_agent(agent_id) -> acknowledgement
```

- `spawn_agent` is `unconfined` because the child may use Bash, MCP, or other unconfined tools. It
  requires approval under the normal default policy.
- The four management/query tools are `read_only` in v1.
- No unbounded blocking `wait_agent` tool exists.

## Spawn behavior

`spawn_agent`:

1. checks a fixed maximum delegation depth;
2. resolves the requested definition or the default;
3. creates a new Session with `origin="subagent"`, parent lineage, and no fork point;
4. builds the child Tool registry and increments delegation depth;
5. creates a child Event bus and Trace writer;
6. starts the normal `send_message()` coroutine as an `asyncio.Task`;
7. registers the task and returns an Agent ID immediately.

The child Session is durable and can later be opened directly. Parent lineage does not import the
parent Entry path into child Context.

The child reuses the parent's Hook/Approval Policy instance. Current approval mode and in-process
“allow for session” choices are therefore shared naturally; a separate approval-bubbling protocol is
not introduced.

## Agent registry

```python
@dataclass
class SpawnedAgent:
    agent_id: str
    owner_run_id: str
    parent_agent_id: str | None
    task: str
    status: Literal["running", "completed", "failed", "cancelled"]
    result: str | None
    task_handle: asyncio.Task
    idle_event: asyncio.Event
    steer_queue: deque[str]
```

`AgentRegistry` is cwd-scoped runtime infrastructure. It:

- assigns Agent IDs;
- enforces a bounded number of concurrent children;
- records Run ownership and parent-agent lineage for lifecycle cleanup;
- updates status and result when tasks finish;
- provides lookup and listing;
- cancels all live children during runtime shutdown.

It is an in-memory index, not the persistence source. Sessions and Traces remain the durable record.

## Checking and waiting

`check_agent` waits on the child's `asyncio.Event` with a bounded timeout:

- the default behaves like a near-instant status check;
- a caller may wait a little longer when the result is immediately required;
- timeout returns current status rather than blocking the parent indefinitely;
- waiting uses an Event, not polling sleeps.

## Non-destructive steering

`steer_agent` appends text to the child's steering queue. `Hooks.inject_messages` drains that queue
at the next Step boundary and appends the messages to the same ongoing Run.

Steering does not cancel the child's current action, discard progress, or create a new Run. A full
redirection is expressed by closing the child and spawning another.

## Lifecycle

Each spawned task belongs to the Run that created it. A child that delegates further work remains
part of that Run's descendant tree; registry ownership must make cleanup selective rather than
cancelling unrelated tasks.

Before a parent Run exposes any terminal result (`COMPLETED`, `MAX_STEPS`, `FAILED`, or
`CANCELLED`), its service cancels and awaits every unfinished task in that descendant tree. A
normally completed child keeps its result, Session, and Trace. An unfinished child therefore never
continues producing side effects after its parent Run has returned to the Host.

`close_agent` closes the selected child and any live descendants it owns. Runtime shutdown cancels
and awaits every remaining child across all Run ownership scopes as a final fallback. Background
work that is intended to outlive a Run is a separate durable-job capability, not a Subagent.

Child progress is not streamed into the parent's transcript. The parent uses check/list for coarse
status; the complete child Session can be resumed independently.

## Required tests

- definition discovery, override, validation, and default selection;
- Model-visible definition filtering and exact-name rejection without child creation;
- isolated child Context and durable parent lineage;
- model and Tool registry precedence;
- maximum delegation depth and concurrent-agent limit;
- non-blocking spawn;
- bounded Event-based check behavior;
- descendant cleanup for every parent Run terminal status without affecting unrelated ownership
  scopes;
- completion, failure, cancellation, recursive close, and shutdown cleanup;
- non-destructive steering at the next Step boundary;
- shared approval mode without bypass through delegation;
- child Session and Trace persistence;
- Scripted Models for parent and children with no live network.

Peer/Team collaboration, destructive steering, and live child streams are deferred; see
[`../deferred.md`](../deferred.md).
