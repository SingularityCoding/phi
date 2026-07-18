# Phi Reference Roadmap

> **Status:** Planning document. Only the capabilities listed as implemented are present in code.

## v1 objective

Phi v1 is a complete, usable, inspectable Agent Harness reference implementation. It should expose
the protocol and control boundaries that frameworks commonly hide while remaining suitable for
later course extraction.

## Current implementation

Implemented today:

- Python 3.12 `src` layout managed by `uv`;
- environment-backed settings for the LiteLLM Proxy;
- Typer entry point with bare invocation;
- minimal Textual application shell;
- stateless OpenAI-compatible Model gateway with HTTP and SSE normalization;
- standalone response assembly, typed Model failures, and model discovery;
- deterministic Scripted Model plus offline protocol tests and opt-in live contracts;
- asynchronous FileSystem and Shell protocols with typed file and process failures;
- canonical workspace Confinement with protected repository and dotenv paths;
- schema-derived Tool registration, strict argument validation, Approval Policy, and async dispatch;
- confined `read`, `write`, `edit`, `grep`, `find`, and `ls` Tools plus explicitly unconfined `bash`;
- bounded asynchronous Runs with immutable Step results, streaming Events, behavioral Hooks,
  sequential Tool round trips, failure policy, and cancellation propagation;
- versioned durable Session entry journals with crash-aware commits, resume, exact forks, branch
  navigation, model selection, isolated Subagent lineage, and separate redacted Event Traces;
- immutable Context construction and inspection, deterministic request estimates, provider Usage
  anchors, manual/threshold/overflow compaction, and bounded context-limit retry;
- cwd-scoped root Project Instructions, validated global/project Agent Skill discovery, deterministic
  stable-instruction assembly, trusted user activation, and read-only Model activation through the
  common Tool Registry and Dispatcher;
- Ruff, ty, pytest, coverage, and pre-commit infrastructure;
- smoke tests for CLI and TUI startup.

The next implementation boundary is the remaining runtime integration: stdio MCP.

## v1 capability scope

| Capability | Design | Implementation |
| --- | --- | --- |
| OpenAI-compatible Model, errors, streaming, assembler | Complete | Implemented |
| Scripted Model and offline protocol tests | Complete | Implemented |
| Confined Environment and file/process result types | Complete | Implemented |
| Tool registry, dispatcher, approvals, built-in tools | Complete | Implemented |
| Bounded Run, Events, Hooks, cancellation | Complete | Implemented |
| Session entries, JSONL storage, resume, fork | Complete | Implemented |
| Context construction and compaction | Complete | Implemented |
| Project instructions and Agent Skills | Complete | Implemented |
| stdio MCP tools, resources, prompts, configuration | Complete | Not started |
| Delegation-style multi-agent tools | Complete | Not started |
| Headless CLI and complete Textual TUI | Complete | Minimal shell only |
| Offline tests, opt-in contracts, behavioral evals | Partial | Model, Environment, Tool, Harness, Session, Context, Project Instructions, and Skills coverage implemented |

## Implementation sequence

1. **Model boundary** — normalized request/response types, errors, Scripted Model, non-streaming HTTP,
   SSE streaming, and response assembly.
2. **Environment and tools** — confined file operations, tool schema generation, validation,
   approval, timeout, execution, and built-ins.
3. **Harness core** — bounded Run, streaming Event forwarding, Hooks, failure semantics, and
   cancellation.
4. **Sessions and Context** — durable entries, tree materialization, Context construction,
   compaction, resume, and fork.
5. **Runtime integrations** — project instructions, Skills, stdio MCP, and cwd-scoped bootstrap.
6. **Subagents** — isolated child Sessions, Agent registry, spawn/check/steer/list/close tools, and
   lifecycle cleanup.
7. **Hosts** — headless commands, shared services, full Textual transcript, approvals, slash
   commands, Queue/Steer interaction, and Context inspector.
8. **Hardening** — contract tests, behavioral evaluations, persistence corruption cases, security
   cases, cancellation races, and documentation verification.

Each stage should land as a tested vertical capability. Do not pre-create later packages as empty
placeholders.

## v1 completion criteria

- A user can configure an allowed model and run Phi through both headless CLI and Textual TUI.
- Model streaming and tool calls normalize into the same final response types as non-streaming
  calls.
- Runs are bounded, cancellable, observable, and deterministic under a Scripted Model.
- File tools respect workspace confinement; unconfined tools are accurately identified and gated.
- Sessions can be resumed and forked without losing common history.
- Context can be inspected and compacted without mutating project instructions.
- Skills and stdio MCP sources feed the same Tool registry.
- Subagents run in isolated Sessions and cannot outlive runtime shutdown.
- Default tests are offline; live contracts are explicit opt-ins.
- README claims match code and tests.

## Implementation details intentionally left open

These decisions may be made when the relevant capability lands:

- the exact checks and output format of `phi doctor`;
- how the default `ConfinedEnvironment.root` is selected and overridden;
- final small-file placement for support values such as directory and execution result types;
- the detailed contract-test and behavioral-evaluation matrix;
- CLI flag spelling where the operation itself is already specified.

Capabilities deliberately outside v1 are tracked in [`deferred.md`](deferred.md).
