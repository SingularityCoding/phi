# Phi Design Documentation

This directory contains the current engineering design for the Phi Reference implementation.
Planned behavior is not implemented behavior; consult code and tests before making user-facing
claims.

## Start here

- [`architecture.md`](architecture.md) — system layers, target `src/phi` layout, and dependencies
- [`roadmap.md`](roadmap.md) — v1 scope, current status, and implementation order
- [`deferred.md`](deferred.md) — capabilities deliberately deferred from v1
- [`references.md`](references.md) — design sources, comparisons, and attribution boundaries
- [`../CONTEXT.md`](../CONTEXT.md) — canonical project vocabulary

## System design

- [`design/model.md`](design/model.md) — Model protocol and OpenAI-compatible gateway
- [`design/run-loop.md`](design/run-loop.md) — bounded Run, streaming, Events, and Hooks
- [`design/tools-and-safety.md`](design/tools-and-safety.md) — tool runtime, approvals, Environment,
  and built-ins
- [`design/sessions-and-context.md`](design/sessions-and-context.md) — entries, session tree,
  Context, instructions, and compaction
- [`design/skills.md`](design/skills.md) — Skill discovery and invocation
- [`design/mcp.md`](design/mcp.md) — MCP integration
- [`design/agents.md`](design/agents.md) — delegation and Subagents
- [`design/hosts.md`](design/hosts.md) — CLI, TUI, slash commands, and interaction behavior

## Historical input

- [`course-design.md`](course-design.md) is an early course-first draft and is not current
  engineering-design authority.

## Course site

- [`course/`](course/) contains the MkDocs teaching-site prototype. It is kept alongside the
  project documentation for repository organization, but it is not engineering design authority.
  The final lessons and exercise cuts will be derived from a stable Phi Reference release.
