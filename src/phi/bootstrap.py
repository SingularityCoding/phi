from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from phi.environment import ConfinedEnvironment
from phi.instructions import ProjectInstructions, load_project_instructions
from phi.skills import (
    SkillDiagnostic,
    SkillDiscovery,
    build_skill_tool,
    discover_skills,
    invoke_user_skill,
    render_model_skill_menu,
)
from phi.tools import ApprovalPolicy, ToolDispatcher, ToolRegistry, build_default_registry


@dataclass(frozen=True)
class RuntimeResources:
    """Resources assembled once for one active working directory."""

    cwd: Path
    project_instructions: ProjectInstructions
    skill_discovery: SkillDiscovery
    stable_instructions: str
    environment: ConfinedEnvironment
    tools: ToolRegistry
    dispatcher: ToolDispatcher
    diagnostics: tuple[SkillDiagnostic, ...]

    def invoke_skill(self, name: str) -> str:
        """Select an already loaded Skill through the trusted user route."""

        return invoke_user_skill(self.skill_discovery.skills, name)


class CwdRuntimeBootstrap:
    """Own and reuse the active cwd-scoped runtime resource collection."""

    def __init__(
        self,
        *,
        base_instructions: str,
        approval_policy: ApprovalPolicy,
        personal_instructions: str = "",
        global_skill_root: Path | None = None,
        default_tool_timeout_seconds: float = 30.0,
    ) -> None:
        self._base_instructions = base_instructions
        self._personal_instructions = personal_instructions
        self._global_skill_root = global_skill_root
        self._approval_policy = approval_policy
        self._default_tool_timeout_seconds = default_tool_timeout_seconds
        self._active: RuntimeResources | None = None

    def load(self, cwd: Path, *, rebuild: bool = False) -> RuntimeResources:
        """Return cached resources for one cwd or rebuild them explicitly."""

        canonical_cwd = cwd.expanduser().resolve(strict=True)
        if not rebuild and self._active is not None and self._active.cwd == canonical_cwd:
            return self._active
        resources = build_runtime_resources(
            canonical_cwd,
            base_instructions=self._base_instructions,
            personal_instructions=self._personal_instructions,
            global_skill_root=self._global_skill_root,
            approval_policy=self._approval_policy,
            default_tool_timeout_seconds=self._default_tool_timeout_seconds,
        )
        self._active = resources
        return resources


def assemble_stable_instructions(
    *,
    base_instructions: str,
    personal_instructions: str,
    project_instructions: ProjectInstructions,
    skill_discovery: SkillDiscovery,
) -> str:
    """Compose the one stable prompt prefix shared by inspection and execution."""

    sections = (
        _instruction_section("PHI BASE INSTRUCTIONS", base_instructions),
        _instruction_section("PERSONAL INSTRUCTIONS", personal_instructions),
        _instruction_section("PROJECT INSTRUCTIONS", project_instructions.content),
        _instruction_section(
            "MODEL-INVOKABLE SKILLS",
            render_model_skill_menu(skill_discovery.skills),
        ),
    )
    return "\n\n".join(section for section in sections if section is not None)


def build_runtime_resources(
    cwd: Path,
    *,
    base_instructions: str,
    approval_policy: ApprovalPolicy,
    personal_instructions: str = "",
    global_skill_root: Path | None = None,
    project_skill_root: Path | None = None,
    default_tool_timeout_seconds: float = 30.0,
) -> RuntimeResources:
    """Build Project Instructions, Skills, Tools, and stable Context input for a cwd."""

    environment = ConfinedEnvironment(cwd)
    canonical_cwd = environment.root
    project_instructions = load_project_instructions(canonical_cwd)
    discovery = discover_skills(
        global_root=(global_skill_root or Path("~/.phi/skills")).expanduser(),
        project_root=project_skill_root or canonical_cwd / ".phi" / "skills",
        project_ignore_root=canonical_cwd,
    )
    stable_instructions = assemble_stable_instructions(
        base_instructions=base_instructions,
        personal_instructions=personal_instructions,
        project_instructions=project_instructions,
        skill_discovery=discovery,
    )
    registry = build_default_registry()
    skill_tool = build_skill_tool(discovery.skills)
    if skill_tool is not None:
        registry.register(skill_tool)
    dispatcher = ToolDispatcher(
        registry,
        approval_policy,
        trusted_values={
            "filesystem": environment.filesystem,
            "shell": environment.shell,
        },
        default_timeout_seconds=default_tool_timeout_seconds,
    )
    return RuntimeResources(
        cwd=canonical_cwd,
        project_instructions=project_instructions,
        skill_discovery=discovery,
        stable_instructions=stable_instructions,
        environment=environment,
        tools=registry,
        dispatcher=dispatcher,
        diagnostics=discovery.diagnostics,
    )


def _instruction_section(label: str, content: str) -> str | None:
    if not content.strip():
        return None
    ending = "" if content.endswith("\n") else "\n"
    return f"--- BEGIN {label} ---\n{content}{ending}--- END {label} ---"
