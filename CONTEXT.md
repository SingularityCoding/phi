# Phi

Phi is a complete Agent Harness reference implementation whose stable releases can later be adapted
into teaching editions. This glossary defines the language used across the project.

## Language

**Phi Reference**:
The complete engineering implementation of Phi and the source from which teaching artifacts are
derived.
_Avoid_: course sample, teaching toy

**Course Edition**:
A deliberately reduced teaching version derived from a stable Phi Reference release.
_Avoid_: Phi Reference

**Agent**:
A Model combined with a Harness, capable of taking bounded action in an Environment.
_Avoid_: model, chatbot

**Model**:
A stateless component that proposes output or an action for one request.
_Avoid_: Agent, Harness

**Usage**:
Provider-reported token counts for one completed Model request.
_Avoid_: Token Estimate, Context size, authoritative billing record

**Token Estimate**:
A non-authoritative approximation of the tokens in a proposed Context, used for budgeting when
provider-reported Usage cannot describe that future request.
_Avoid_: Usage, exact token count

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

**Entry**:
One durable node in a Session's conversation tree.
_Avoid_: event, trace record

**Conversation View**:
The messages and effective settings materialized from one path through a Session's Entry tree.
_Avoid as domain model names_: State, Transcript

**Context**:
The finite projection of a Conversation View sent to the Model for one request.
_Avoid_: Conversation View, Trace, Memory

**Memory**:
Selected information retained and retrieved across Run or Session boundaries according to an
explicit persistence and retrieval policy.
_Avoid_: Context, Session, Conversation View

**Session**:
A durable tree of Entries that supports resuming and branching a conversation.
_Avoid_: Run, Context

**Event**:
A notification describing something that occurred while Phi was operating.
_Avoid_: Hook, Entry

**Hook**:
An explicit extension point whose result may alter Harness behavior.
_Avoid_: Event, listener

**Trace**:
A developer-facing observation record produced by consuming Events.
_Avoid_: Session, Context

**Skill**:
A discoverable instruction resource that can be selected by a user or made available to the Model.
_Avoid_: Tool, Agent Definition

**Agent Definition**:
A discoverable description of a specialized Subagent's prompt, tools, and model preferences.
_Avoid_: Skill, Session

**Subagent**:
An Agent with an isolated Context that performs a delegated task on behalf of a parent Agent.
_Avoid_: peer, teammate
