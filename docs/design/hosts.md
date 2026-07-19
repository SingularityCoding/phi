# CLI and TUI Hosts Design

> **Status:** Design complete; persistent `phi run` and the minimal Textual shell are implemented.
> The remaining CLI commands and complete TUI are pending.

## Host boundary

Typer CLI and Textual TUI are separate adapters over the same bootstrap and Session services. Hosts
may parse arguments, collect interactive choices, subscribe to Events, and render results. They do
not implement Context construction, Session persistence, Tool dispatch, or the Run loop.

Cwd-scoped bootstrap resources are built once. The current immutable `SessionHandle` is owned by the
Host and replaced after new/resume/fork/send operations.

Headless mode should land before the full TUI because it is the smallest consumer that can prove
shared services do not depend on Textual.

## CLI command surface

```text
phi
phi run "<task>" [--session <id>] [--json] [--max-steps N] [--model <name>]
phi session list
phi session resume <id>
phi session fork <id> <entry_id> [--model <name>]
phi context [--session <id>] [--json]
phi mcp add <name> -- <command> [args...] [--global]
phi mcp list [--global]
phi mcp remove <name> [--global]
phi doctor
```

- Bare `phi` launches the TUI with a new Session.
- `phi run` is a persistent headless Run; without `--session` it creates a Session.
- `--json` attaches an Event listener that writes one serialized event per stdout line.
- Headless approval allows read-only tools and denies workspace mutation and unconfined tools unless
  an explicit mode says otherwise.
- `session resume` launches the TUI on the chosen Session.
- CLI `session fork` creates a branch and exits; interactive fork continues inside the TUI.
- `context` calls the same Context builder used before a model request and performs no model call.
- `doctor` validates configuration and Proxy connectivity; its exact checks remain an implementation
  detail.

Exit codes for `phi run`:

| Run status | Exit code |
| --- | --- |
| `COMPLETED` | 0 |
| `FAILED` | 1 |
| `MAX_STEPS` | 2 |
| `CANCELLED` | 130 |

## TUI layout

```text
Screen
├── TranscriptView (scrollable, fills remaining space)
├── queued-message area
├── PromptInput (multiline)
└── StatusBar
```

The presentation widget named `TranscriptView` dispatches Entry and Event types to focused widgets.
“Transcript” is a UI label here, not a second canonical conversation data model:

- `UserMessageView`;
- `AssistantMessageView`, with Markdown and collapsed reasoning;
- `ToolCallView`, shown as a spinner during execution and updated in place on completion;
- `CompactionEntryView` as a structural summary marker.

The status bar shows model, Session name/ID, cwd, and idle/running state.

## Streaming

The TUI subscribes to the shared Event bus:

- `ContentDelta` and `ReasoningDelta` append to the current assistant view;
- partial Tool Call JSON is not rendered;
- `ToolCallStarted` creates the tool card after assembly;
- `ToolCallCompleted` updates that card with output or error.

Streaming observation does not change the normalized response stored by the Harness.

## Prompt input, Queue, and Steer

The Prompt is a multiline TextArea:

- Enter submits;
- Shift+Enter inserts a newline;
- `/` opens slash-command completion;
- Escape cancels the active Run through `asyncio.Task.cancel()`.

While a Run is active, submitted follow-ups appear in a queue above the Prompt. Each may be edited,
removed, or marked as:

- **queue** — send as a new request after the active Run ends;
- **steer** — inject at the next Step boundary through `Hooks.inject_messages`.

Steer does not cancel the active Run. Escape cancels only that Run; it does not reinterpret or clear
ordinary queued input. After the active Run reaches any terminal status, including `CANCELLED`, the
Host first awaits its cancellation and owned-resource cleanup, then sends ordinary queued messages
in FIFO order. Each message starts a separate new Run, and each Run finishes before the next queued
message is sent.

An ordinary queued message remains editable or removable and is not a Session Entry until the Host
actually sends it. Discarding queued user input requires an explicit remove action rather than Run
cancellation.

## Slash commands

Slash commands are TUI routing and interaction, not a second application command framework.

Built-ins:

| Command | Operation |
| --- | --- |
| `/new` | create a Session |
| `/resume [id]` | resume, with selector when omitted |
| `/fork [entry_id]` | fork, with history selector when omitted |
| `/tree` | switch to another existing leaf |
| `/session` | show current Session metadata |
| `/name <text>` | set Session display name |
| `/context` | open Context inspector |
| `/mcp` | show connected MCP servers and tool counts |
| `/model` | choose the branch model |
| `/compact [focus]` | manually compact Context |
| `/permissions` | select approval mode |
| `/quit` | exit |

Each loaded Skill adds `/skill-name`. Each MCP prompt adds
`/mcp__server__prompt-name`. These dynamic commands invoke the same underlying Skill and MCP
operations described in their design documents.

Login/logout, trust, extension reload, exports, sharing, copying, changelog, hotkey help, and a
general settings screen are not v1 commands because they do not correspond to a current Harness
capability.

## Context inspector

`/context` opens a modal containing:

- complete system prompt;
- every registered Tool name, description, and full JSON Schema;
- every selected message, including Tool Calls and Tool Results;
- dropped-history summary when present;
- character counts and any available aggregate real token usage.

Sections are collapsible. Phi does not invent exact per-section token counts.

## Interactive approval

When approval policy resolves an internal `ask`, the TUI uses
`ApprovalPromptScreen(ModalScreen)` with:

- Allow once;
- Allow for session;
- Deny.

The Run must execute in a Textual worker because `push_screen_wait()` cannot safely block an ordinary
message handler. The modal may be visually compact, but it is a Screen result boundary rather than
an inline transcript widget.

The resolver returns a final allow/deny decision. “Allow for session” also updates the policy's
in-memory Tool-name allowance.

## Model selection

`/model`, `phi run --model`, and Session fork model selection all use
`list_available_models()`. If the current branch already contains model output, selecting another
model implicitly forks rather than changing the model mid-branch.

## Required tests

- every CLI command remains a thin call into shared services;
- exit-code mapping and JSON Event output;
- headless fail-closed approval;
- TUI startup, Session switching, and transcript rendering;
- streaming widget updates and Tool card lifecycle;
- FIFO Queue processing after every terminal status, including `CANCELLED`;
- Steer injection, queued-message editing/removal, and Escape independence;
- slash-command routing and dynamic Skill/MCP commands;
- Context inspector completeness;
- approval modal worker behavior and session allowance;
- model selection and implicit fork behavior;
- cancellation cleanup of Models, MCP transports, and child agents.
