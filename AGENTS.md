# Phi Coding Agent Guide

## Scope and project intent

These instructions apply to the entire `phi` repository. Phi lives inside a multi-repository
workspace, but it is an independent Git repository with its own dependencies and history. Run all
commands from this repository root and do not modify or synchronize sibling projects unless the
user explicitly asks.

Phi is being developed in two stages. The first stage focuses on building a nearly complete, well-architected, testable, and maintainable Agent reference implementation without prematurely simplifying it for teaching. Once that implementation is stable, the second stage will adapt it into a course by removing selected key components for students to implement, based on factors such as class size, student background, learning objectives, and course duration. The resulting course materials, exercises, and assessment criteria will be published in a separate GitHub repository. Phi is currently in the first stage, where the priority is completing the reference Agent rather than creating the student version.

The central definition is:

> **Agent = Model + Harness**

Use the canonical vocabulary in [`CONTEXT.md`](CONTEXT.md). In particular, do not use Model, Agent,
Harness, Run, Session, Context, Conversation View, Event, Hook, Trace, Tool Call, or Tool Result as
interchangeable terms.

## Sources of truth

Use these sources according to their role:

| Source | Authority |
| --- | --- |
| Implemented code and tests | Current behavior; highest authority when documents disagree |
| `CONTRIBUTING.md` | Current setup, commands, validation, and test policy |
| `CONTEXT.md` | Canonical domain language |
| `docs/architecture.md` | System layers, ownership, dependency direction, and target layout |
| `docs/design/*.md` | Long-lived design contracts for each capability |
| `docs/roadmap.md` | What exists now, what comes next, and v1 completion criteria |
| `README.md` | User-facing claims about behavior available now |
| `docs/course/` | Teaching prototype; not engineering-design authority |
| `docs/course-design.md` | Historical course-first input; not current design authority |

Read the relevant design document before implementing its capability. A design document describes
the destination, not proof that the code exists. When docs disagree, prefer code and tests, then
current architecture/design documents, then historical material.

## Runtime settings

See `CONTRIBUTING.md` for repository setup and how to launch Phi (`uv run phi`, currently a minimal
Textual shell — press `q` to exit). Commands mentioned only in `docs/design/` are unavailable until
their code and tests land.

Runtime settings use the `PHI_` prefix and may be placed in `.env`:

| Variable | Purpose |
| --- | --- |
| `PHI_BASE_URL` | OpenAI-compatible LiteLLM Proxy base URL |
| `PHI_API_KEY` | LiteLLM virtual key |
| `PHI_DEFAULT_MODEL` | Default proxy model name |
| `PHI_REQUEST_TIMEOUT_SECONDS` | Model request timeout in seconds |

Defaults and parsing behavior live in `src/phi/settings.py`; `.env.example` is the copyable
template. Do not assume that a design document's future setting is already accepted by the current
`Settings` model.

## Development commands

`Taskfile.yml` provides these shortcuts:

| Command | Effect |
| --- | --- |
| `task --list` | List available tasks |
| `task setup` | Run locked sync and create `.env` only when missing |
| `task lint` | Run Ruff lint checks |
| `task format-check` | Check formatting without changing files |
| `task typecheck` | Run ty |
| `task test -- <pytest args>` | Run pytest with optional focused arguments |
| `task check` | Run all required non-mutating handoff checks |
| `task fix` | Apply safe Ruff fixes and formatting; this modifies files |
| `task pre-commit` | Run all prek hooks; some hooks may modify files |

Prefer the non-mutating checks while inspecting or reviewing. Use `task fix` or autofixing hooks
only when edits are in scope, then review every resulting diff.

## Testing and validation

`CONTRIBUTING.md` is authoritative for the required validation suite, test policy, and when a
documentation-only change can skip it — run `task check` (or the equivalent `uv run` commands listed
there) before handoff.

`pytest` is configured to discover tests under `tests/`, enable `pytest-asyncio` auto mode, measure
branch coverage across `src`, and show missing lines. There is currently no enforced minimum
coverage percentage; do not invent one.

Useful focused forms include:

```bash
uv run pytest tests/test_cli.py
uv run pytest tests/test_app.py
uv run pytest tests/test_cli.py -k bare
```

Name test modules `test_*.py` and test functions `test_*`. Place shared fixtures in `conftest.py`
only when more than one test genuinely shares them.

The only current GitHub Actions workflow validates the course site, so it does not replace local
Python checks.

## Architecture invariants

- Keep one installable distribution and one top-level Python namespace: `phi`.
- The Model is a thin, stateless protocol boundary. It does not own history, Context assembly,
  tool execution, Sessions, or UI behavior.
- The Harness owns the bounded control loop, in-run state, tool authorization and execution,
  failure policy, Events, Hooks, cancellation, and stopping decisions.
- Durable conversation history belongs to Sessions. A finite Context is projected from a
  Conversation View. A Trace is a persisted observation of Events. Keep these representations
  distinct.
- The Environment provides ground truth through files, processes, tests, and external services.
- Typer CLI and Textual TUI are thin Hosts. Shared operations must call the same underlying service
  rather than being reimplemented per Host.
- The Model may propose a Tool Call, but only the Harness may validate, authorize, execute, retry,
  or reject it.
- Events are notifications and cannot alter behavior. Hooks are explicit interception points that
  may alter behavior.
- Streaming and non-streaming Model paths must assemble the same normalized final response.
- Multi-agent delegation must compose the existing Session, Run, Tool, Hook, and Event primitives;
  it must not introduce a second hidden agent loop.
- Evaluate outcomes against Environment state and fail closed when approval, confinement, or
  execution state is uncertain.

Preserve the negative dependency rules in `docs/architecture.md`: Model must not depend on Harness,
Sessions, or Hosts; Harness must not depend on Sessions, Hosts, Skills, MCP, or Subagents; Hosts
must not duplicate application behavior; and Subagents must delegate through existing Session and
Run services.

## Model gateway and trust boundaries

Phi communicates with the course LiteLLM Proxy through OpenAI-compatible HTTP and SSE. The adapter
owns transport and protocol normalization, not Agent behavior. Do not add the LiteLLM Python SDK
unless a later documented decision introduces a direct-provider use case.

Tool schemas may be sent to the Model, but model output is untrusted input. Validate arguments,
apply approval policy, enforce timeouts, and return typed Tool Results at the dispatcher boundary.
File-system confinement is honest only for operations routed through the confined Environment;
shell, MCP, and Subagent capabilities are not automatically path-confined. Phi does not claim OS
sandboxing.

Never commit, print, log, trace, fixture, screenshot, or document a real API key or virtual key.
Keep secrets in the ignored `.env` file or the external execution environment. Do not silently
replace a missing credential with another identity or endpoint.

## Python and code conventions

- Use Python 3.12 features and complete type annotations at public boundaries.
- Use absolute imports from `phi`; avoid relative imports between project packages.
- Ruff targets Python 3.12, enforces a 100-character line length, and enables `E`, `F`, `I`, `UP`,
  and `B` rules. Formatting is owned by Ruff.
- Prefer standard-library dataclasses for trusted in-memory values and Pydantic for untrusted
  parsing boundaries such as environment configuration and persisted Session data.
- Keep OpenAI-compatible wire dictionaries at the adapter boundary. Use explicit internal types
  for Model responses, Tool Calls, Usage, Run results, Entries, and Events.
- Prefer async boundaries for network I/O, streaming, cancellation, tools, MCP, and Subagents.
  Adapt synchronous tool handlers behind the async dispatcher instead of blocking the loop.
- Treat recoverable failures as typed data where the design requires recovery. Reserve a failed
  Run for a failure the loop cannot safely handle.
- Keep `__init__.py` small and export only deliberate public APIs.
- Keep CLI callbacks thin and Textual widgets focused on presentation and interaction. Business
  behavior belongs in shared services.
- Avoid speculative abstractions. Follow the target dependency direction when the first real
  consumer justifies a new module.

For substantial Textual work, repository-specific architecture and the installed project versions
take precedence over generic examples. Test UI behavior with Textual's headless `run_test()`
support rather than relying only on manual terminal interaction.

## Change workflow

1. Run `git status --short --branch` and preserve all pre-existing user changes.
2. Identify the requested capability and inspect its current code, tests, roadmap status, and
   relevant design document before editing.
3. Keep the change inside this repository and implement the smallest complete vertical slice that
   respects the architecture boundaries.
4. Add or update deterministic tests alongside behavior. Do not create later-stage packages merely
   to match the target tree.
5. Update documentation according to its authority: `README.md` for available user behavior,
   `docs/roadmap.md` for implementation status, and design docs only when the durable contract
   changes.
6. Run focused checks during development, then the required handoff suite. Finish with
   `git diff --check` and review `git diff` for accidental or generated changes.

Use a short-lived feature spec when a concrete implementation slice needs acceptance criteria; do
not turn the long-lived system design into an implementation checklist. Record an ADR only for a
consequential, hard-to-reverse decision chosen from genuine alternatives.

Do not hand-edit or commit `.venv/`, `site/`, `dist/`, `__pycache__/`, `.coverage`,
`.pytest_cache/`, `.ruff_cache/`, or other generated artifacts. Do not copy a reference
implementation wholesale; borrow ideas intentionally, preserve required attribution, and keep
Phi's boundaries explicit.

There is currently no enforced repository-specific PR title or commit-message format. Do not
commit, push, publish, or deploy unless the user asks. At handoff, report the files changed, checks
run, and any checks or live integrations not run.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues in `SingularityCoding/phi`, managed via the `gh` CLI. See
`docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles map 1:1 to label strings of the same name (`needs-triage`,
`needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` at the repo root, ADRs under `docs/adr/`. See
`docs/agents/domain.md`.
