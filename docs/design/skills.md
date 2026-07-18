# Agent Skills Design

> **Status:** Core discovery, Context menu, Model Tool, and trusted user invocation implemented;
> Host-generated slash commands remain a later stage.

## Purpose

Skills are discoverable instruction resources that can be loaded on demand without placing every
instruction body in the Model's Context. They integrate through existing Context and Tool
mechanisms; they are not a new architectural layer.

## Format

A Skill is either:

- a directory containing `SKILL.md`; or
- a standalone Markdown file in a configured Skill root.

Frontmatter includes:

- `name`;
- `description`;
- optional `disable-model-invocation`.

```python
@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str
    source_path: Path
    disable_model_invocation: bool = False
```

## Discovery and validation

Phi searches:

1. global Skills under `~/.phi/skills/`;
2. project Skills under `.phi/skills/`.

Project definitions override global definitions with the same name. Discovery is recursive and
respects ignore-file behavior. Names must match the containing directory where applicable and use a
bounded lowercase alphanumeric/hyphen form. Descriptions are required and bounded.

One invalid Skill produces a warning diagnostic and is skipped; it does not prevent other Skills
from loading. This tolerant batch behavior is intentionally different from loading one corrupted
Session file.

## Invocation

The system prompt contains a menu of Model-invocable Skill names and descriptions. A dedicated
read-only `skill_tool` returns already loaded content by name.

The tool does not ask the Model to use the normal file reader because global Skills live outside the
workspace confinement root and because a dedicated call is clearer in Events and Trace.

`disable_model_invocation=True` applies at both Model-facing boundaries:

- the Skill is omitted from the Model-visible menu;
- `skill_tool` rejects the Skill even when the Model supplies its exact name, producing a
  `ToolResult.error` without returning any part of the Skill content.

It does not prevent a user from selecting the Skill explicitly. The generated TUI slash command
uses a trusted user-invocation route rather than calling `skill_tool`, so it may return the same
loaded content.

This field controls Skill invocation, not access to equivalent bytes through unrelated tools. Phi
does not detect or specially block an independently authorized tool such as `bash` from reading a
Skill source that the tool can otherwise reach; that operation remains governed by the tool's own
approval and confinement rules.

Each loaded Skill also generates a TUI slash command. Model invocation through `skill_tool` and user
invocation through a slash command are separate routes to the same content.

## Required tests

- directory and standalone-file discovery;
- frontmatter and naming validation;
- warning-and-skip behavior for one malformed definition;
- ignore rules;
- global loading and project override;
- Model-visible menu filtering;
- `disable_model_invocation` rejection by exact name without content disclosure;
- retained user invocation of a Model-disabled Skill;
- Skill tool Event visibility and lack of workspace-file access.
