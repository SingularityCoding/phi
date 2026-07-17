# Phi Design Documentation

This directory contains the current engineering design for the Phi Reference implementation.
Planned behavior is not implemented behavior; consult code and tests before making user-facing
claims.

## Start here

- [`architecture.md`](architecture.md) — system layers, target `src/phi` layout, and dependencies
- [`roadmap.md`](roadmap.md) — v1 scope, current status, and implementation order
- [`deferred.md`](deferred.md) — capabilities deliberately deferred from v1
- [`../CONTEXT.md`](../CONTEXT.md) — canonical project vocabulary

## Capability specifications

- [`specs/model.md`](specs/model.md) — Model protocol and OpenAI-compatible gateway
- [`specs/run-loop.md`](specs/run-loop.md) — bounded Run, streaming, Events, and Hooks
- [`specs/tools-and-safety.md`](specs/tools-and-safety.md) — tool runtime, approvals, Environment,
  and built-ins
- [`specs/sessions-and-context.md`](specs/sessions-and-context.md) — entries, session tree,
  Context, instructions, and compaction
- [`specs/skills.md`](specs/skills.md) — Skill discovery and invocation
- [`specs/mcp.md`](specs/mcp.md) — MCP integration
- [`specs/agents.md`](specs/agents.md) — delegation and Subagents
- [`specs/hosts.md`](specs/hosts.md) — CLI, TUI, slash commands, and interaction behavior

## Historical input

- [`course-design.md`](course-design.md) is an early course-first draft and is not an engineering
  specification.

## Course site

- [`course/`](course/) contains the MkDocs teaching-site prototype. It is kept alongside the
  project documentation for repository organization, but it is not engineering design authority.
  The final lessons and exercise cuts will be derived from a stable Phi Reference release.
