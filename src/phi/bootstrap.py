from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from phi.environment import ConfinedEnvironment
from phi.harness import EventEmitter
from phi.instructions import ProjectInstructions, load_project_instructions
from phi.mcp import (
    McpConfigDiagnostic,
    McpConfigError,
    McpDiagnostic,
    McpEvent,
    McpPrompt,
    McpPromptResult,
    McpRuntime,
    connect_mcp_servers,
    load_merged_mcp_config,
)
from phi.skills import (
    SkillDiagnostic,
    SkillDiscovery,
    build_skill_tool,
    discover_skills,
    invoke_user_skill,
    render_model_skill_menu,
)
from phi.tools import ApprovalPolicy, ToolDispatcher, ToolRegistry, build_default_registry

type RuntimeDiagnostic = SkillDiagnostic | McpConfigDiagnostic | McpDiagnostic


@dataclass(frozen=True)
class RuntimeResources:
    """Resources assembled once for one active working directory."""

    cwd: Path
    project_instructions: ProjectInstructions
    skill_discovery: SkillDiscovery
    stable_instructions: str
    environment: ConfinedEnvironment
    mcp: McpRuntime
    tools: ToolRegistry
    dispatcher: ToolDispatcher
    diagnostics: tuple[RuntimeDiagnostic, ...]

    def invoke_skill(self, name: str) -> str:
        """Select an already loaded Skill through the trusted user route."""

        return invoke_user_skill(self.skill_discovery.skills, name)

    async def list_mcp_prompts(self, server_id: str | None = None) -> tuple[McpPrompt, ...]:
        """List cached MCP Prompts through the trusted runtime route."""

        return await self.mcp.list_prompts(server_id)

    async def get_mcp_prompt(
        self,
        command: str,
        arguments: dict[str, str],
    ) -> McpPromptResult:
        """Retrieve one user-selected MCP Prompt without mutating a Session."""

        return await self.mcp.get_prompt(command, arguments)

    async def close(self) -> None:
        """Close every long-lived cwd-scoped resource exactly once."""

        await self.mcp.close()

    async def __aenter__(self) -> RuntimeResources:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()


class CwdRuntimeBootstrap:
    """Own and reuse the active cwd-scoped runtime resource collection."""

    def __init__(
        self,
        *,
        base_instructions: str,
        approval_policy: ApprovalPolicy,
        personal_instructions: str = "",
        global_skill_root: Path | None = None,
        global_mcp_config_path: Path | None = None,
        event_bus: EventEmitter[McpEvent] | None = None,
        default_tool_timeout_seconds: float = 30.0,
    ) -> None:
        self._base_instructions = base_instructions
        self._personal_instructions = personal_instructions
        self._global_skill_root = global_skill_root
        self._global_mcp_config_path = global_mcp_config_path
        self._event_bus = event_bus
        self._approval_policy = approval_policy
        self._default_tool_timeout_seconds = default_tool_timeout_seconds
        self._active: RuntimeResources | None = None
        self._lock = asyncio.Lock()

    async def load(self, cwd: Path, *, rebuild: bool = False) -> RuntimeResources:
        """Return cached resources for one cwd or rebuild them explicitly."""

        canonical_cwd = cwd.expanduser().resolve(strict=True)
        async with self._lock:
            if not rebuild and self._active is not None and self._active.cwd == canonical_cwd:
                return self._active
            previous = self._active
            self._active = None
            if previous is not None:
                await previous.close()
            resources = await build_runtime_resources(
                canonical_cwd,
                base_instructions=self._base_instructions,
                personal_instructions=self._personal_instructions,
                global_skill_root=self._global_skill_root,
                global_mcp_config_path=self._global_mcp_config_path,
                approval_policy=self._approval_policy,
                event_bus=self._event_bus,
                default_tool_timeout_seconds=self._default_tool_timeout_seconds,
            )
            self._active = resources
            return resources

    async def close(self) -> None:
        """Close the active cwd lifetime and clear the cache."""

        async with self._lock:
            active = self._active
            self._active = None
            if active is not None:
                await active.close()

    async def __aenter__(self) -> CwdRuntimeBootstrap:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()


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


async def build_runtime_resources(
    cwd: Path,
    *,
    base_instructions: str,
    approval_policy: ApprovalPolicy,
    personal_instructions: str = "",
    global_skill_root: Path | None = None,
    project_skill_root: Path | None = None,
    global_mcp_config_path: Path | None = None,
    project_mcp_config_path: Path | None = None,
    event_bus: EventEmitter[McpEvent] | None = None,
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
    config_diagnostics: tuple[McpConfigDiagnostic, ...] = ()
    try:
        mcp_config = await load_merged_mcp_config(
            (global_mcp_config_path or Path("~/.phi/mcp.json")).expanduser(),
            project_mcp_config_path or canonical_cwd / ".phi" / "mcp.json",
        )
    except McpConfigError as error:
        mcp = McpRuntime()
        config_diagnostics = (error.diagnostic,)
    else:
        mcp = await connect_mcp_servers(
            mcp_config,
            cwd=canonical_cwd,
            registry=registry,
            events=event_bus,
        )
    try:
        dispatcher = ToolDispatcher(
            registry,
            approval_policy,
            trusted_values={
                "filesystem": environment.filesystem,
                "shell": environment.shell,
            },
            default_timeout_seconds=default_tool_timeout_seconds,
        )
    except BaseException:
        await mcp.close()
        raise
    return RuntimeResources(
        cwd=canonical_cwd,
        project_instructions=project_instructions,
        skill_discovery=discovery,
        stable_instructions=stable_instructions,
        environment=environment,
        mcp=mcp,
        tools=registry,
        dispatcher=dispatcher,
        diagnostics=(*discovery.diagnostics, *config_diagnostics, *mcp.diagnostics),
    )


def _instruction_section(label: str, content: str) -> str | None:
    if not content.strip():
        return None
    ending = "" if content.endswith("\n") else "\n"
    return f"--- BEGIN {label} ---\n{content}{ending}--- END {label} ---"
