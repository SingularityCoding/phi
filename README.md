<div align="center">
  <img src="./assets/banner.png" alt="Phi — Agent = Model + Harness" width="100%" />

  <h1>Phi</h1>

  <p><strong>An inspectable Agent Harness, built from scratch in Python.</strong></p>

  <p>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12" /></a>
    <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/uv-managed-DE5FE9?style=flat-square&logo=uv&logoColor=white" alt="Managed with uv" /></a>
    <a href="https://typer.tiangolo.com/"><img src="https://img.shields.io/badge/Typer-CLI-009485?style=flat-square" alt="Typer CLI" /></a>
    <a href="https://textual.textualize.io/"><img src="https://img.shields.io/badge/Textual-TUI-8025E8?style=flat-square" alt="Textual TUI" /></a>
    <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-C28F5C?style=flat-square" alt="MIT License" /></a>
  </p>
</div>

Phi is an engineering-quality Python reference implementation of an Agent Harness. It keeps the
control loop, tool execution, context construction, session lifecycle, and safety boundaries
explicit—so every part of an agent can be understood, tested, and extended.

“From scratch” means implementing the Harness itself, not training a language model or delegating
the core loop to an agent framework.

## The idea

Phi starts from one definition:

<p align="center"><strong>Agent = Model + Harness</strong></p>

The Model is a stateless protocol boundary that proposes responses and actions. The Harness owns
the bounded loop: it builds Context, governs tools, observes the Environment, manages failures, and
decides when a Run stops.

| Boundary | Responsibility |
| --- | --- |
| **Model** | Normalize OpenAI-compatible requests, responses, streaming, and tool calls |
| **Harness** | Control Runs, Steps, tools, Events, Hooks, failures, and stopping decisions |
| **Environment** | Provide observable ground truth through files, processes, tests, and services |
| **Hosts** | Expose the same runtime through a Typer CLI and a Textual TUI |

## Core capabilities

Phi's v1 design brings the complete agent runtime together through a set of explicit, composable
capabilities:

- **Model gateway** — OpenAI-compatible HTTP and SSE transport, normalized response types, and a
  deterministic Scripted Model for offline tests.
- **Tools and safety** — schema-driven tools, validation, approvals, timeouts, execution policy,
  and honest workspace-confinement boundaries.
- **Harness loop** — bounded, cancellable Runs with streaming Events, behavioral Hooks, and typed
  completion and failure semantics.
- **Sessions and Context** — durable conversation trees, resume and fork workflows, budgeted
  Context construction, and compaction without deleting history.
- **Runtime integrations** — project instructions, on-demand Agent Skills, and stdio MCP tools,
  resources, and prompts.
- **Delegation** — isolated Subagent Sessions built from the same Run, Tool, Event, and Hook
  primitives as the parent Agent.
- **Developer experience** — a headless CLI and interactive TUI backed by the same application
  services rather than separate implementations.

The Model gateway, Tool processing boundary, and Harness core are implemented today. In addition to
streaming and non-streaming Model transport, Phi now has a schema-derived Tool registry, strict
local argument validation, Approval Policy presets, asynchronous dispatch with timeout and
cancellation semantics, and seven built-in Tools. The public Harness operation runs every ordinary
Step through the streaming Model protocol, processes complete Tool Calls sequentially, emits
ordered lifecycle Events, applies behavioral Hooks, and returns a bounded immutable Run Result.

`read`, `write`, `edit`, `grep`, `find`, and `ls` route file access through a FileSystem that
canonically resolves paths, confines them to an explicit workspace root, and denies protected Git
metadata and dotenv paths by default. `bash` is deliberately different: it starts in the workspace
and is governed by approval and timeout, but it is unconfined and is not an operating-system
sandbox. File Confinement is an in-process structural check, not protection against every
filesystem race. The CLI and TUI remain a minimal shell while Sessions, Context, and later roadmap
stages are built; the Model, Tool, and Harness boundaries are not yet wired into an interactive
Session.

## Design principles

- Keep the Model stateless and the Hosts thin.
- Make control flow, authority, and failure policy visible in ordinary Python.
- Default to deterministic, offline tests and evaluate outcomes against Environment state.
- Fail closed when approval, confinement, or execution state is uncertain.
- Add abstractions when their first working capability arrives.

## Project guide

- [Architecture](docs/architecture.md) — system layers, dependency direction, and target package map
- [System design](docs/README.md) — the detailed contract for each capability
- [Roadmap](docs/roadmap.md) — v1 scope, implementation sequence, and completion criteria
- [Project glossary](CONTEXT.md) — the canonical language used across Phi
- [Course site prototype](docs/course/index.md) — an early teaching surface derived from the design

> This README will evolve alongside Phi as the project is developed.
