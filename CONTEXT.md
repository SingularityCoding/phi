# Phi

Phi is a complete Agent Harness reference implementation whose stable releases can later be adapted
into teaching editions. This glossary defines the language used across the project.

## Language

### Project and course

**Phi Reference**:
The complete engineering implementation of Phi and the source from which teaching artifacts are
derived.
_Avoid_: course sample, teaching toy

**Course Edition**:
A deliberately reduced teaching version derived from a stable Phi Reference release.
_Avoid_: Phi Reference

### Agent, Model, and Harness

**Agent**:
A Model combined with a Harness, capable of taking bounded action in an Environment.
_Avoid_: model, chatbot

**Model**:
A stateless component that proposes output or an action for one request.
_Avoid_: Agent, Harness

**Harness**:
The authority that constructs bounded model interactions, controls Runs, governs tools, and decides
when work stops.
_Avoid_: model, host, UI

**Environment**:
The external source of ground truth in which observable work and side effects occur.
_Avoid_: Context, Trace

**Host**:
A user-facing adapter that invokes and observes Phi, such as the CLI or TUI.
_Avoid_: Harness, Agent

### Runs, Tools, and Safety

**Run**:
One bounded attempt by the Harness to handle a user request.
_Avoid_: session, model call

**Step**:
One model request and response together with the tool round trips produced by that response.
_Avoid_: Run, tool call

**Tool Call**:
A structured action proposed by the Model for the Harness to consider.
_Avoid_: tool execution

**Tool Result**:
The structured outcome returned after the Harness processes a Tool Call.
_Avoid_: model response

**Approval Policy**:
The rule set the Harness consults to allow or deny a proposed Tool Call before it executes.
_Avoid_: permission, sandboxing, confinement

**Confinement**:
The structural guarantee that file operations routed through the Environment stay inside the
workspace root.
_Avoid_: sandboxing, approval

### Events, Hooks, and Trace

**Event**:
A notification describing something that occurred while Phi was operating.
_Avoid_: Hook, Entry

**Hook**:
An explicit extension point whose result may alter Harness behavior.
_Avoid_: Event, listener

**Steer**:
Non-destructive injection of a new message into an ongoing Run at the next Step boundary, without
cancelling current work.
_Avoid_: interrupt, cancel, queue

**Trace**:
A developer-facing observation record produced by consuming Events.
_Avoid_: Session, Context

### Sessions and Context

**Session**:
A durable tree of Entries that supports resuming and branching a conversation.
_Avoid_: Run, Context

**Entry**:
One durable node in a Session's conversation tree.
_Avoid_: event, trace record

**Conversation View**:
The messages and effective settings materialized from one path through a Session's Entry tree.
_Avoid as domain model names_: State, Transcript

**Context**:
The finite projection of a Conversation View sent to the Model for one request.
_Avoid_: Conversation View, Trace, Memory

**Fork**:
Branching a Session at a chosen Entry into a new Session that references the parent's history
instead of copying it.
_Avoid_: copy, clone

**Compaction**:
Replacing older Entries in a Conversation View with a generated summary so a request stays within
the Context window, triggered manually, by threshold, or by overflow.
_Avoid_: truncation, trimming

**Usage**:
Provider-reported token counts for one completed Model request.
_Avoid_: Token Estimate, Context size, authoritative billing record

**Token Estimate**:
A non-authoritative approximation of the tokens in a proposed Context, used for budgeting when
provider-reported Usage cannot describe that future request.
_Avoid_: Usage, exact token count

**Memory**:
Selected information retained and retrieved across Run or Session boundaries according to an
explicit persistence and retrieval policy. Deferred in v1 — Phi has no Memory store, capture, or
retrieval implementation yet.
_Avoid_: Context, Session, Conversation View

### Skills and Subagents

**Skill**:
A discoverable instruction resource that can be selected by a user or made available to the Model.
_Avoid_: Tool, Agent Definition

**Agent Definition**:
A discoverable description of a specialized Subagent's prompt, tools, and model preferences.
_Avoid_: Skill, Session

**Subagent**:
An Agent with an isolated Context that performs a delegated task on behalf of a parent Agent.
_Avoid_: peer, teammate

**Delegation**:
The parent-to-child pattern where an Agent spawns an isolated Subagent for one task, as opposed to
peer or team collaboration among equal Agents.
_Avoid_: peer collaboration, team collaboration
