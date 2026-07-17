# Phi Project Guide

## Project intent

Phi is a complete, engineering-quality reference implementation of a Python Agent Harness that
will later be adapted into teaching material. Build the reference implementation first; derive the
course edition, exercises, and lesson plan from a stable Phi release rather than constraining the
runtime to a provisional class schedule.

“From scratch” means implementing the Harness, not training a language model. Do not replace the
core learning path with LangChain, Agno, an Agents SDK, or another framework that owns the loop.

Phi uses this definition:

> **Agent = Model + Harness**

The canonical project vocabulary is defined in [`CONTEXT.md`](CONTEXT.md).

## Architecture invariants

- Keep one installable distribution and one top-level namespace: `phi`.
- The Model is a thin, stateless protocol boundary. It must not own conversation history, context
  assembly, tool execution, sessions, or UI behavior.
- The Harness owns the bounded control loop, in-run state, tool authorization and execution,
  failure policy, events, hooks, and stopping decisions.
- Durable conversation history belongs to Sessions. A finite Context is projected from the current
  conversation view; a Trace is a persisted observation of Events. Do not conflate them.
- The Environment provides ground truth through files, processes, tests, and external services.
- Typer CLI and Textual TUI are Hosts. They must stay thin and must not own the canonical loop or
  durable conversation state.
- When CLI commands and TUI slash commands expose the same operation, they must call the same
  underlying service.
- Tool schemas may be proposed to the Model, but only the Harness may validate, authorize,
  execute, retry, or reject a tool call.
- Events are notifications. Hooks may affect behavior. Do not mix interception semantics into the
  event stream.
- Model streaming must assemble the same normalized final response as non-streaming requests.
- Multi-agent delegation must compose the existing Session, Run, Tool, Hook, and Event primitives;
  it must not introduce a second hidden agent loop.
- Create packages and abstractions when their first real capability arrives. The target package map
  is a dependency guide, not an instruction to create empty modules.

The complete package map and dependency graph live in
[`docs/architecture.md`](docs/architecture.md).

## Model gateway boundary

Phi speaks OpenAI-compatible HTTP to the course LiteLLM Proxy. Do not add the LiteLLM Python SDK
unless a later documented decision introduces a direct-to-provider use case. The adapter owns HTTP
and SSE transport plus protocol normalization; it does not own Agent behavior.

Never commit, print, log, or place a real virtual key in tests or documentation.

## Engineering principles

- Use Python 3.12 features and complete type annotations at public boundaries.
- Prefer explicit internal types for model responses, tool calls, usage, run results, entries, and
  events. Keep OpenAI-compatible wire dictionaries at the adapter boundary.
- Use standard-library dataclasses for trusted in-memory values. Use Pydantic at untrusted parsing
  boundaries such as environment configuration and persisted session data.
- Prefer async boundaries for network I/O, streaming, cancellation, tools, MCP, and multi-agent
  work. Sync tool handlers may be adapted behind the async dispatcher boundary.
- Treat expected failures as typed data where the design calls for recovery; reserve failed Runs
  for failures the loop cannot safely handle.
- Default to deterministic, offline tests. Use a Scripted Model that records requests and fails when
  its response script is exhausted.
- Evaluate Agent outcomes against Environment state, not against the model's claim that work is
  complete.
- Default to fail closed whenever approval, confinement, or execution state is uncertain.
- Keep `__init__.py` files small and expose only deliberate public APIs.
- Use absolute imports from `phi`.

## Documentation authority

- `README.md` is for users and describes only behavior that exists now.
- `CONTRIBUTING.md` is for contributors and owns current setup, commands, and validation workflow.
- `CONTEXT.md` is the implementation-free project glossary.
- `docs/architecture.md` describes system structure and dependency direction.
- `docs/specs/` contains the current implementation design for each capability.
- `docs/roadmap.md` records implementation status and sequencing.
- `docs/deferred.md` records deliberately deferred capabilities and the reasons for deferring them.
- `docs/course-design.md` is a historical course-first draft, not current engineering design
  authority.

When documents disagree, implemented code and tests win, followed by current specifications and
architecture documents, then historical material. Record an ADR only for a consequential decision
that is hard to reverse, surprising without context, and chosen from genuine alternatives.

## Agent workflow and safety

- Follow [`CONTRIBUTING.md`](CONTRIBUTING.md) for current setup and validation commands.
- Inspect `git status --short --branch` before editing and preserve user changes.
- Keep changes scoped to this repository and the requested capability.
- Do not hand-edit generated files or caches.
- Do not copy a reference implementation wholesale. Borrow ideas intentionally, retain required
  attribution, and keep Phi's own boundaries explicit.
- Do not run live model acceptance checks without an explicit need; they consume shared external
  resources and require credentials.
- Treat file, shell, MCP, and subagent execution as capability boundaries. File-system confinement
  applies only where the implementation actually routes access through the confined Environment;
  do not claim it protects unconfined tools.
