# CLI and TUI Hosts Design

> **Status:** Implemented.

## Host boundary

Typer CLI and Textual TUI are separate adapters over the same bootstrap and Session services. Hosts
may parse arguments, collect interactive choices, subscribe to Events, and render results. They do
not implement Context construction, Session persistence, Tool dispatch, or the Run loop.

Cwd-scoped bootstrap resources are built once. The current immutable `SessionHandle` is owned by the
Host and replaced after new/resume/fork/send operations.

The headless Host landed first as the smallest consumer proving shared services do not depend on
Textual; the complete TUI now composes those same services interactively.

## CLI command surface

```text
phi
phi run "<task>" [--session <id>] [--json] [--max-steps N] [--model <name>]
phi session list
phi session resume <id>
phi session fork <id> <entry_id> [--model <name>]
phi context [--session <id>] [--json]
phi mcp add [--global] <name> -- <command> [args...]
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
- `doctor` validates trusted Model Settings and credentials, Proxy model discovery, and configured
  default-Model availability in dependency order without building an Agent runtime.

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
├── StatusBar
├── TranscriptView (scrollable, fills remaining space)
├── queued-message area
├── slash-command completion
├── PromptInput (multiline)
└── ComposerHint
```

The presentation widget named `TranscriptView` dispatches Entry and Event types to focused widgets.
“Transcript” is a UI label here, not a second canonical conversation data model:

- `UserMessageView`;
- `AssistantMessageView`, with Markdown and a lightweight collapsed `ReasoningView` immediately
  above the content or Tool Call produced by the same Model Step;
- `ToolCallView`, shown as a spinner during execution and updated in place on completion;
- `CompactionEntryView` as a structural summary marker;
- `RunBoundaryView`, which renders successful completion as a muted horizontal rule centered on the
  local `HH:MM` completion time. Cancelled and Step-limited Runs include their outcome in the rule;
  failures remain explicit error cards.

The top status bar keeps durable orientation separate from input affordances. It shows the Session
name or a shortened ID, selected Model, Approval mode, and a live estimated Context-capacity signal.
The normal signal is the utilization percentage relative to the effective input limit. Terminals at
least 120 columns wide additionally show the Token Estimate, effective limit, and safe prompt limit;
`~` marks the estimate. Below 80 and 55 columns, first Session and then Approval are progressively
omitted. The workspace path is available through `/session` rather than occupying the persistent
bar. While a Run is active Context reads `updating`; after a Run or Session-changing command it is
rebuilt from the current immutable handle. If the effective limit is unknown, the Host shows the
estimated token count and `limit unknown` without inventing a percentage. Approaching 80% of the
safe prompt limit adds an explicit warning label and semantic warning color. The one-line bar
truncates safely at every width.

## Streaming

The TUI subscribes to the shared Event bus:

- `ContentDelta` appends to the current assistant view;
- `ReasoningDelta` reveals and appends to the collapsed reasoning view immediately above it;
- partial Tool Call JSON is not rendered;
- `ToolCallStarted` creates the tool card after assembly;
- `ToolCallCompleted` updates that card with output or error.

Streaming observation does not change the normalized response stored by the Harness.

## Prompt input, Queue, and Steer

The Prompt is a multiline TextArea. It is three rows tall for a one-line draft, grows with hard or
soft-wrapped content, and stops at eight rows so the Transcript retains useful space:

- Enter submits;
- Shift+Enter inserts a newline;
- `/` opens slash-command completion;
- Escape cancels the active Run through `asyncio.Task.cancel()`.

The main Screen does not render Textual's persistent Footer. A one-line Composer hint exposes only
the controls relevant to the current state: send/newline/command discovery while idle; Queue and
cancel behavior while a Run is active; and command execution while slash completion is visible.
`Ctrl+P` still opens the command palette and `Ctrl+Q` still quits. Run completion remains visible in
the Transcript boundary rather than being repeated indefinitely in either persistent bar.

While a Run is active, submitted follow-ups appear in a compact, single-line queue above the Prompt.
Embedded newlines are folded into the preview and long content truncates safely. Each may be edited,
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

Slash-command completion is capped at six rows. It remains immediately above the Prompt, uses the
Composer hint for its execution affordance, and disappears as soon as the draft is no longer a
single slash-command token.

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

`/context` opens a full-screen, read-only request explorer over one immutable inspection snapshot.
It does not call the Model, append Entries, compact Context, or change the selected leaf. The
explorer has exactly three views:

- **Overview** is the default and teaches the projection `Session path → Conversation View →
  Context → normalized Model request`. It shows meaningful counts at each boundary, the selected
  model, major input sources, estimate provenance, effective input limit, and safe prompt limit.
- **Contents** presents a navigable hierarchy of trusted instruction origins, complete registered
  Tool definitions, selected messages, and the generated dropped-history summary. Tool Calls and
  Tool Results use semantic labels; selecting an item shows complete Model-visible content or JSON
  Schema plus source, inclusion state, and character count.
- **Raw request** renders the exact frozen normalized `ModelRequest` snapshot, including messages,
  tools, model, temperature, and maximum output tokens.

Instruction origin labels come from bootstrap assembly metadata rather than parsing prompt
delimiters. The explorer uses direct view keys, Tree arrow navigation, and Escape to close. On narrow
terminals the Contents tree and detail pane stack vertically and remain scrollable. Phi does not
invent exact per-section token counts and does not present cumulative provider Usage as current
Context size.

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
- Context status honesty and refresh behavior, plus Context explorer completeness, keyboard
  navigation, immutability, and narrow-terminal layout;
- approval modal worker behavior and session allowance;
- model selection and implicit fork behavior;
- cancellation cleanup of Models, MCP transports, and child agents.
