# Deferred Capabilities

This document records capabilities that are worth considering but deliberately excluded from Phi
v1. They are not forgotten requirements. Reconsider them only when the stated trigger appears.

## Per-call price display

LiteLLM can expose real cost for some non-streaming responses through
`X-Litellm-Response-Cost`, but the value is unavailable when streaming headers are sent. Scoped
virtual keys also cannot access the management endpoints needed for consistent after-the-fact cost
reporting. Users can inspect billing in the Proxy's management interface.

**Reconsider when:** the Proxy exposes one permission-safe, streaming-compatible cost API.

## Selective parallel tool execution

Phi v1 executes multiple Tool Calls from one model response sequentially. One considered design was
to let each Tool declare `parallel_safe` and use `asyncio.gather()` only when every call in the
batch declared itself safe. Phi does not introduce that contract yet: a Tool's claim about side
effects and concurrency safety would need precise ordering, failure, cancellation, approval, and
Event semantics, while sequential execution is simple and deterministic.

This does not close off later concurrency. The dispatcher remains async, async handlers are awaited,
and sync handlers are adapted through worker threads. What is deferred is the public safety contract
and concurrent scheduling policy, not the async execution boundary.

**Reconsider when:** measured tool latency justifies defining and testing ordering and concurrency
semantics.

## Operating-system sandboxing

Phi v1 does not implement macOS Seatbelt, Linux Landlock/seccomp, or platform-equivalent kernel
isolation. Such a sandbox would contain tool implementations even when they have bugs, but it would
also require separate platform integrations and a substantially larger implementation and
maintenance surface across macOS, Linux, and Windows.

The v1 choice is deliberately narrower. `ConfinedEnvironment` provides in-process structural
protection only for file tools that route access through its `FileSystem`; it is not a substitute
for kernel enforcement. Unconfined tools such as `bash`, MCP tools, and `spawn_agent` remain governed
by cwd, timeout, and approval, and Phi must not claim that those controls create an OS sandbox.

**Reconsider when:** Phi must execute untrusted commands with guarantees stronger than in-process
policy can provide.

## Model-facing MCP tool discovery layer

One considered design was to expose only fixed `mcp_search` and `mcp_execute` meta-tools, using a
BM25 discovery index so the Model searches for an MCP Tool before invoking it. This can keep a large
MCP catalog from filling the Model's tool-schema budget.

Phi instead directly registers connected MCP tools as `mcp__{server_id}__{tool_name}` because v1 is
not expected to connect enough servers for catalog size to justify an indirect layer. Direct
registration is simpler and preserves concrete tool identity in ordinary Tool Call Events and Trace
without extra normalization; observers see the actual server and tool rather than only a generic
`mcp_execute` call at that surface. MCP connection-time discovery itself remains part of v1.

**Reconsider when:** connected tool schemas measurably exhaust Context budget or degrade tool
selection.

## Remote MCP transport and OAuth

Phi v1 supports stdio MCP servers only. Streamable HTTP/SSE transport, OAuth callbacks, token
refresh, and remote reconnection add a separate authentication and lifecycle surface.

**Reconsider when:** a required integration cannot be supplied as a local stdio server.

## Local JSON Schema validation for MCP arguments

MCP argument schemas come from remote servers. Phi forwards arguments and turns server rejection
into a Tool Result instead of adding a second `jsonschema` dependency and validation path.

**Reconsider when:** remote validation latency or error quality becomes a demonstrated problem.

## Ripgrep or shell-backed read-only search tools

The built-in `grep`, `find`, and `ls` use the confined `FileSystem`, even though a native ripgrep or
shell implementation would be faster. Calling shell commands would weaken the structural guarantee
implied by their `read_only` approval class.

**Reconsider when:** large-repository performance becomes unacceptable and confinement can remain
honest under the faster implementation.

## Peer or Team multi-agent collaboration

Phi v1 implements Delegation: a parent can spawn an isolated Subagent, inspect it, and provide
non-destructive steering. It does not implement peer mailboxes, shared task lists, or equal agents
that coordinate among themselves.

**Reconsider when:** Phi needs long-lived peer collaboration rather than parent-to-child delegation.

## Destructive Subagent steering

`steer_agent` injects a message at the next Step boundary without discarding current work. It does
not interrupt the current action and restart the child Run.

**Reconsider when:** non-destructive steering plus close-and-respawn cannot express a real workflow.

## Live child-agent progress in the parent transcript

Child Sessions keep their own Events, Trace, and durable history. The parent sees coarse status
through agent tools rather than interleaving multiple live streams in one transcript.

**Reconsider when:** the TUI has a clear design for concurrent child streams and users need live
inspection more than post-run Session review.

## Composable tool capability sets

One considered design was to replace the three-way Tool classification with a composable set such
as `reads_files`, `writes_files`, `network`, and `executes_code`. It arose because an earlier
`Tool.access` design called its third bucket `network`, which inaccurately described tools such as
`bash` that can combine file access, networking, and arbitrary process execution.

Phi resolves the current problem by naming the field `approval_class` and the third bucket
`unconfined`. The three values (`read_only`, `mutates_workspace`, and `unconfined`) are explicitly
coarse approval buckets aligned with the current confinement boundary, not a complete inventory of
everything a Tool can do. Current `ApprovalRule` matching by Tool-name glob plus bucket does not use
finer combinations such as network-read versus network-write, so a capability-set abstraction would
add schema, policy, and testing complexity without a current consumer.

**Reconsider when:** approval rules need combinations that the three current buckets cannot express.

## Long-term Memory

Phi v1 does not implement a separate Memory store or automatic policies for capturing, updating,
retrieving, ranking, or forgetting information across Runs or Sessions. A Session preserves a
conversation branch, and Context projects part of that branch into one Model request; neither is a
curated Memory. Project instructions and Skills are explicitly authored resources rather than facts
learned automatically from conversation.

Memory also requires product decisions that v1 does not yet need: scope and ownership, provenance,
write timing, conflict correction, retrieval ranking, retention, deletion, and privacy. Those
policies should be designed around a demonstrated reuse workflow rather than inferred from Session
persistence.

**Reconsider when:** users need selected information to be retrieved across Runs independently of
full branch history, or shared across Session branches, and project instructions or Skills are not
an adequate explicit home for it.
