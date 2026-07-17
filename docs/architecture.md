# Phi Architecture

> **Status:** Target architecture for the Phi Reference implementation. Packages are created only
> when their first working capability lands.

## System model

Phi follows one central definition:

> **Agent = Model + Harness**

The system is organized into five logical layers:

```text
Hosts: Typer CLI / Textual TUI
                │
        Application services
                │
             Harness ───────── Environment
                │
              Model
                │
     OpenAI-compatible LiteLLM Proxy
```

- **Hosts** parse user interaction and render results. They do not own the loop or durable state.
- **Application services** connect the current Session, Context construction, Harness, tools, and
  runtime resources. In the Python package these services are deliberately split between
  `bootstrap.py` and `sessions/service.py`; there is no separate `phi.application` package.
- **Harness** owns the bounded Run, in-run state, tool decisions, Events, Hooks, failure policy, and
  stopping decisions.
- **Model** converts OpenAI-compatible transport data into normalized Phi values and remains
  unaware of the Harness.
- **Environment** provides observable ground truth through files and processes.

Logical layers do not have to map one-for-one to nested Python directories. Tools, Sessions, Skills,
MCP, and Subagents are explicit capability packages composed into the Harness.

## Target `src/phi` layout

```text
src/phi/
├── __init__.py
├── settings.py                 # Environment-backed application configuration
├── bootstrap.py                # cwd-scoped runtime wiring
├── instructions.py             # Project instruction discovery
├── model/
│   ├── types.py
│   ├── events.py
│   ├── errors.py
│   ├── assembler.py
│   ├── protocol.py
│   ├── openai_compatible.py
│   ├── scripted.py
│   └── registry.py
├── environment/
│   ├── protocol.py
│   └── confined.py
├── tools/
│   ├── types.py
│   ├── approval.py
│   └── builtin/
├── skills/
│   ├── types.py
│   └── discovery.py
├── mcp/
│   ├── config.py
│   ├── client.py
│   └── tools.py
├── agents/
│   ├── definition.py
│   ├── registry.py
│   └── tools.py
├── sessions/
│   ├── entries.py
│   ├── metadata.py
│   ├── storage.py
│   └── service.py
├── harness/
│   ├── run.py
│   ├── hooks.py
│   ├── events.py
│   ├── context.py
│   └── compaction.py
├── cli/
└── ui/
```

This is a target map, not a scaffold to materialize up front. Small support types should live near
their first consumer; do not create a generic `utils`, `common`, or project-wide type dump.

## Why there is no `phi.application`

The logical Application Services layer contains two different kinds of work:

1. `bootstrap.py` constructs cwd-scoped infrastructure such as Settings, the confined Environment,
   built-in tools, loaded Skills, MCP connections, and the Agent registry.
2. `sessions/service.py` composes durable session storage with Context construction and
   `harness.run()` through operations such as `create_session()`, `resume_session()`,
   `fork_session()`, and `send_message()`.

Keeping `send_message()` with Sessions also preserves the multi-agent dependency direction:
`phi.agents` can start an isolated child Session without importing a layer above the Harness or
introducing a second loop.

## Dependency direction

```text
model, environment
        ↓
      tools
        ↓
 skills, mcp

sessions data/storage ──→ model
harness ────────────────→ model + tools + environment
sessions/service ───────→ sessions + harness + model + tools
agents ─────────────────→ tools + sessions/service
mcp ────────────────────→ tools + harness event surface
bootstrap ──────────────→ settings + environment + tools + skills + mcp + agents
cli, ui ────────────────→ bootstrap + sessions/service + harness public types
```

Required negative dependencies:

- `phi.model` does not import Harness, Sessions, Typer, or Textual.
- `phi.harness` does not import Sessions, Hosts, Skills, MCP, or Subagents.
- `phi.environment` does not import Hosts or Harness.
- `phi.cli` and `phi.ui` do not duplicate application behavior.
- `phi.agents` delegates by calling the existing Session and Run services; it does not implement a
  hidden loop.

`harness/context.py` consumes an already materialized Conversation View, Tool specs, and stable
instructions; it does not accept a `SessionHandle`. `harness/compaction.py` owns pure threshold,
cut-selection, and summary-request policy. `sessions/service.py` owns Entry traversal, invokes those
policies, and persists `CompactionEntry`. This split preserves the rule that Harness does not import
Sessions.

## Runtime ownership

The Host owns a current immutable `SessionHandle`. Cwd-scoped infrastructure is built once and
reused while the working directory remains unchanged. A Session switch replaces the handle without
rescanning Skills or reconnecting MCP servers.

Long-lived resources must have an explicit async lifetime:

- HTTP clients;
- MCP subprocesses and client sessions;
- Event and Trace writers;
- running child-agent tasks.

Every child-agent task is registered under the Run that spawned it. Before a parent Run returns any
terminal result to its Host, the owning service cancels and awaits that Run's unfinished descendant
tasks without affecting children owned by another Run. Subagents do not become background jobs
that outlive their parent Run.

On runtime shutdown, the registry cancels and awaits any remaining child agents across all Run
ownership scopes, then transports are closed. Global singletons are not part of the design.

## Conversation and observability boundaries

- **Entries** are the durable conversation tree.
- A **Conversation View** is materialized from one path through that tree.
- **Context** is a budgeted projection sent to the Model.
- **Events** describe work in progress and cannot alter behavior.
- **Hooks** are named interception points that may alter behavior.
- **Trace** is an Event consumer's persisted developer record and is not used to resume a Session.

## Trust boundaries

The Model proposes Tool Calls but has no execution authority. The dispatcher validates arguments,
applies approval policy, enforces timeout, executes the handler, and returns a Tool Result.

`ConfinedEnvironment` provides structural workspace confinement only for tools that route file
access through its `FileSystem` interface. `bash`, MCP tools, and `spawn_agent` are deliberately
classified as unconfined; they are governed by cwd, timeout, and approval but not by path-level
confinement. Phi v1 does not claim operating-system sandboxing.

## Reference implementation and course edition

This repository is the Phi Reference implementation. A Course Edition will later be derived from a
stable release and may prebuild infrastructure, remove advanced capabilities, or introduce focused
implementation gaps. Course sequencing is not an architectural dependency and does not govern the
reference package structure.
