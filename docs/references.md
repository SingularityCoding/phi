# Design References

Phi is an original implementation, but its design was informed by existing Agent Harnesses,
product documentation, open protocols, and framework APIs. This document preserves that research
provenance independently of the current system-design documents.

Being listed here means that a source was consulted; it does not mean Phi adopts the source's full
architecture or endorses every implementation choice. The current Phi design is defined by
[`architecture.md`](architecture.md), [`design/`](design/), and implemented code and tests. If Phi
later incorporates source code rather than an idea or interface comparison, that code must retain
the upstream project's license and file-level attribution as required.

<!-- --8<-- [start:course-references] -->

## Agent Harnesses and coding agents

### Gemma

- Source: [thecarbonlayer/gemma](https://github.com/thecarbonlayer/gemma)
- Consulted for: the Model/Harness boundary, a small teaching-oriented control loop, provider Usage,
  context compaction, deterministic tests, and the progression of Harness primitives.
- Phi's direction: build a complete reference implementation first and derive teaching editions
  later. Phi also adds durable branchable Sessions, explicit Events and Hooks, MCP, Skills, and
  Delegation as compositions of its own primitives.

### Tau

- Source: [huggingface/tau](https://github.com/huggingface/tau), formerly
  `alejandro-ao/tau`
- Documentation: [twotimespi.dev](https://twotimespi.dev/)
- Relevant to Phi for: a small, readable Python coding-agent Harness; separation of provider
  transport, reusable Agent core, and coding Host; typed Event streaming; durable append-only JSONL
  Sessions; Context compaction; project instructions; Skills; and separate Textual and print Hosts.
- Phi's direction: use Tau as a close architectural comparison, not an implementation template.
  Phi retains one top-level `phi` namespace, its own Model/Harness/Session vocabulary and dependency
  graph, explicit approval and confinement boundaries, and its own designs and tests.

### Pi Agent Harness

- Source: [earendil-works/pi](https://github.com/earendil-works/pi), formerly
  `badlogic/pi-mono`
- Consulted for: tree-shaped Session history, parent-linked Entries, compaction boundaries, the
  separation between notification subscriptions and behavior-changing hooks, runtime/service
  ownership, built-in coding tools, and Environment-style file and process interfaces.
- Phi's direction: re-express the useful boundaries in typed Python APIs. Phi chooses stateless
  Session services, an explicit confined file interface, its own Entry variants, and its own
  compaction and approval policies rather than reproducing Pi's runtime.

### SuperQode

- Source: [SuperagenticAI/superqode](https://github.com/SuperagenticAI/superqode)
- Documentation:
  [SuperQode Harness System](https://superagenticai.github.io/superqode/advanced/harness-system/)
- Consulted for: direct and discovery-mediated MCP Tool registration, stdio MCP lifecycle,
  delegation tools, peer-agent alternatives, non-destructive steering queues, Event-based waiting,
  and bounded child-agent concurrency.
- Phi's direction: implement parent-to-child Delegation only. Peer mailboxes, shared team task
  lists, destructive steering, and an MCP search/execute indirection layer remain deferred until
  their complexity has a demonstrated consumer.

### OpenAI Codex

- Source: [openai/codex](https://github.com/openai/codex)
- Consulted for: the distinction between technical sandboxing and approval policy, append-only
  rollout/event records, project instructions, Agent Skills, follow-up input, and Subagent
  lifecycle behavior.
- Focused evidence: [Codex issue #22099](https://github.com/openai/codex/issues/22099) helped
  motivate bounded child-status waiting instead of an unbounded blocking wait operation.
- Phi's direction: preserve separate Environment, approval, Event, Trace, Session, and Delegation
  concepts. Similar user-facing capabilities do not imply identical internal storage or runtime
  ownership.

### Claude Code and Anthropic

- Documentation:
  [prompt caching](https://code.claude.com/docs/en/prompt-caching),
  [custom Subagents](https://code.claude.com/docs/en/sub-agents), and
  [Subagents versus Agent Teams](https://code.claude.com/docs/en/agent-sdk/claude-code-features)
- Compaction references:
  [context editing and compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)
  and the
  [automatic context-compaction cookbook](https://platform.claude.com/cookbook/tool-use-automatic-context-compaction)
- Multi-agent reference:
  [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
- Consulted for: prompt-cache invalidation, stable project instructions, isolated Subagent Context,
  the distinction between Delegation and peer teams, compaction triggers, and the performance value
  of separate context windows.
- Phi's direction: keep model selection stable per Session branch, keep project instructions outside
  summarized history, and implement Delegation without adopting Agent Teams or Claude Code's
  private transcript format.

## Open standards and protocols

### Agent Skills

- Specification: [Agent Skills](https://agentskills.io/)
- Consulted for: `SKILL.md` structure, progressive disclosure, discoverability, and portability
  across Agent Hosts.
- Phi implements the format within its own Context, Tool, approval, and invocation boundaries; the
  standard does not own Phi's Run loop.

### Project instructions

- Convention: [AGENTS.md](https://agents.md/)
- Consulted for: a vendor-neutral project-instruction filename and repository-scoped guidance.
- Phi reads a root `AGENTS.md` and falls back to `CLAUDE.md`; nested instruction discovery is
  outside v1.

### Model Context Protocol

- Specification: [Model Context Protocol](https://modelcontextprotocol.io/specification/2025-11-25)
- SDK: [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- Consulted for: stdio transport, lifecycle and capability negotiation, and the distinct control
  models of Tools, Resources, and Prompts.
- Phi uses the official SDK for protocol mechanics while retaining authorization, Tool adaptation,
  Events, and Session behavior in the Harness. Remote transport and OAuth are deferred.

### OpenTelemetry terminology

- Reference:
  [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- Consulted for: established observability language around Events, Traces, model requests, tool
  calls, and token Usage. Phi's current Trace format is its own JSONL product rather than an
  OpenTelemetry implementation.

## Libraries and framework comparisons

### Model and Tool APIs

- [Pydantic AI](https://github.com/pydantic/pydantic-ai)
- [OpenAI Agents SDK for Python](https://github.com/openai/openai-agents-python)

Their typed Python APIs were compared when selecting async Model boundaries, dataclasses for trusted
runtime values, and function-signature-derived Tool schemas. Phi does not depend on either framework
because owning and exposing the Harness loop is a central project goal.

### Multi-agent patterns

- [OpenAI Agents SDK for Python](https://github.com/openai/openai-agents-python)
- [LangGraph](https://github.com/langchain-ai/langgraph)
- [Google Agent Development Kit](https://github.com/google/adk-python)
- [CrewAI](https://github.com/crewAIInc/crewAI)

These projects were surveyed to compare delegation, handoff, graph, and peer-team patterns. Phi's v1
choice is narrower: Subagents are isolated child Sessions invoked through ordinary Tools and the
same bounded Run loop.

### LiteLLM Proxy

- Documentation: [LiteLLM Proxy quick start](https://docs.litellm.ai/docs/proxy/quick_start)

LiteLLM informed the deployment boundary, not the Harness design. The course Proxy owns provider
routing, virtual-key authentication, budgets, and rate limits; Phi speaks OpenAI-compatible HTTP and
does not embed the LiteLLM Python SDK.

### Textual

- Documentation: [Textual Screens](https://textual.textualize.io/guide/screens/)
- Design discussion:
  [`push_screen_wait` and worker usage](https://github.com/Textualize/textual/discussions/2559)

These references informed the modal approval and Context-inspector interaction boundaries. Textual
remains a Host dependency and does not own the Harness loop or durable Session state.

<!-- --8<-- [end:course-references] -->
