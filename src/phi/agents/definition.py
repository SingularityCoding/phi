from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml

from phi.instruction_discovery import (
    IgnoreRule,
    candidate_paths,
    has_symlink_component,
    initial_ignore_rules,
    is_ignored,
    parse_instruction_file,
)

_AGENT_NAME_PATTERN = re.compile(r"(?=.{1,64}\Z)[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER_FIELDS = {
    "name",
    "description",
    "tools",
    "model",
    "disable-model-invocation",
}


@dataclass(frozen=True)
class AgentDefinition:
    """Validated specialist instructions and optional child authority preferences."""

    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None = None
    model: str | None = None
    disable_model_invocation: bool = False


@dataclass(frozen=True)
class AgentDefinitionDiagnostic:
    """Actionable warning produced while tolerantly discovering Agent Definitions."""

    source_path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.source_path}: {self.reason}"


@dataclass(frozen=True)
class AgentDefinitionDiscovery:
    """Immutable effective Agent Definition collection and diagnostics."""

    definitions: Mapping[str, AgentDefinition]
    diagnostics: tuple[AgentDefinitionDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "definitions", MappingProxyType(dict(self.definitions)))


DEFAULT_AGENT_DEFINITION = AgentDefinition(
    name="general-purpose",
    description="Handle a general delegated task.",
    system_prompt="Complete the delegated task and return a concise, evidence-based result.\n",
)


def discover_agent_definitions(
    *,
    global_root: Path,
    project_root: Path,
    project_ignore_root: Path | None = None,
) -> AgentDefinitionDiscovery:
    """Discover effective global and project Agent Definitions."""

    global_definitions, global_diagnostics = _discover_root(global_root)
    project_rules, ignore_diagnostics = initial_ignore_rules(
        project_ignore_root,
        project_root,
    )
    project_definitions, project_diagnostics = _discover_root(
        project_root,
        initial_rules=project_rules,
    )
    return AgentDefinitionDiscovery(
        definitions={**global_definitions, **project_definitions},
        diagnostics=(
            *global_diagnostics,
            *(
                AgentDefinitionDiagnostic(item.source_path, item.reason)
                for item in ignore_diagnostics
            ),
            *project_diagnostics,
        ),
    )


def _discover_root(
    root: Path,
    *,
    initial_rules: tuple[IgnoreRule, ...] = (),
) -> tuple[dict[str, AgentDefinition], tuple[AgentDefinitionDiagnostic, ...]]:
    if (
        has_symlink_component(root)
        or not root.is_dir()
        or is_ignored(root, is_directory=True, rules=initial_rules)
    ):
        return {}, ()
    definitions: dict[str, AgentDefinition] = {}
    definition_paths: dict[str, Path] = {}
    diagnostics: list[AgentDefinitionDiagnostic] = []
    source_paths, candidate_diagnostics = candidate_paths(
        root,
        package_filename="AGENT.md",
        resource_label="Agent Definition",
        initial_rules=initial_rules,
    )
    diagnostics.extend(
        AgentDefinitionDiagnostic(item.source_path, item.reason) for item in candidate_diagnostics
    )
    for source_path in source_paths:
        try:
            definition = _load_definition(source_path)
        except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
            diagnostics.append(AgentDefinitionDiagnostic(source_path, str(error)))
            continue
        if definition.name in definitions:
            diagnostics.append(
                AgentDefinitionDiagnostic(
                    source_path,
                    f"Agent Definition name collision; first valid definition is "
                    f"{definition_paths[definition.name]}",
                )
            )
            continue
        definitions[definition.name] = definition
        definition_paths[definition.name] = source_path
    return definitions, tuple(diagnostics)


def _load_definition(source_path: Path) -> AgentDefinition:
    parsed = parse_instruction_file(
        source_path,
        resource_label="Agent Definition",
        package_filename="AGENT.md",
        name_pattern=_AGENT_NAME_PATTERN,
    )
    frontmatter = parsed.frontmatter
    unknown_fields = sorted(set(frontmatter) - _FRONTMATTER_FIELDS)
    if unknown_fields:
        raise ValueError(f"Agent Definition has unknown field {unknown_fields[0]!r}")

    system_prompt = parsed.content
    if not system_prompt.strip():
        raise ValueError("Agent Definition instructions must not be empty")
    raw_tools = frontmatter.get("tools")
    tools: tuple[str, ...] | None = None
    if raw_tools is not None:
        if not isinstance(raw_tools, list) or not all(
            isinstance(item, str) and item.strip() for item in raw_tools
        ):
            raise ValueError("Agent Definition tools must be a list of non-empty Tool names")
        tools = tuple(item.strip() for item in raw_tools)
        if len(set(tools)) != len(tools):
            raise ValueError("Agent Definition Tool names must be unique")
    model = frontmatter.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Agent Definition model must be non-empty text")
        model = model.strip()
    disabled = frontmatter.get("disable-model-invocation", False)
    if type(disabled) is not bool:
        raise ValueError("Agent Definition disable-model-invocation must be a boolean")
    return AgentDefinition(
        name=parsed.name,
        description=parsed.description,
        system_prompt=system_prompt,
        tools=tools,
        model=model,
        disable_model_invocation=disabled,
    )
