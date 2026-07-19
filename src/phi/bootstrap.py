from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import httpx

from phi.agents import (
    AgentDefinitionDiagnostic,
    AgentDefinitionDiscovery,
    AgentRuntime,
    build_agent_tools,
    discover_agent_definitions,
)
from phi.environment import ConfinedEnvironment
from phi.harness import EventEmitter
from phi.instructions import PHI_BASE_INSTRUCTIONS, ProjectInstructions, load_project_instructions
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
from phi.model import (
    Model,
    ModelConfig,
    ModelInfo,
    OpenAICompatibleModel,
    list_available_models,
)
from phi.sessions import SessionStorage
from phi.settings import Settings
from phi.skills import (
    SkillDiagnostic,
    SkillDiscovery,
    build_skill_tool,
    discover_skills,
    invoke_user_skill,
    render_model_skill_menu,
)
from phi.tools import (
    HEADLESS_MODE,
    ApprovalPolicy,
    RuleBasedApprovalPolicy,
    Tool,
    ToolDispatcher,
    ToolRegistry,
    build_default_registry,
)

type RuntimeDiagnostic = (
    AgentDefinitionDiagnostic | SkillDiagnostic | McpConfigDiagnostic | McpDiagnostic
)


@dataclass(frozen=True)
class RuntimeResources:
    """Resources assembled once for one active working directory."""

    cwd: Path
    project_instructions: ProjectInstructions
    skill_discovery: SkillDiscovery
    agent_definitions: AgentDefinitionDiscovery
    stable_instructions: str
    environment: ConfinedEnvironment
    mcp: McpRuntime
    agents: AgentRuntime
    agent_tools: tuple[Tool, ...]
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

        try:
            await self.agents.close()
        finally:
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


class HostConfigurationError(ValueError):
    """Trusted Settings cannot produce a usable production Host runtime."""


@dataclass
class HostRuntime:
    """One reusable Host lifetime for Model, Session, and cwd-scoped resources."""

    settings: Settings
    model: Model
    available_models: tuple[ModelInfo, ...]
    storage: SessionStorage
    resources: RuntimeResources
    close_callback: Callable[[], Awaitable[object]] | None = None
    _closed: bool = False

    async def close(self) -> None:
        """Settle Agent/MCP resources before closing the owned Model transport."""

        if self._closed:
            return
        self._closed = True
        resource_error: BaseException | None = None
        try:
            await self.resources.close()
        except BaseException as error:
            resource_error = error
        try:
            if self.close_callback is not None:
                await self.close_callback()
        except BaseException:
            if resource_error is None:
                raise
        if resource_error is not None:
            raise resource_error


def model_config_from_settings(settings: Settings) -> ModelConfig:
    """Construct trusted Model configuration without coupling the Model package to Settings."""

    api_key = settings.api_key.get_secret_value()
    if not api_key.strip():
        raise HostConfigurationError("PHI_API_KEY is required")
    if not settings.base_url.strip():
        raise HostConfigurationError("PHI_BASE_URL must not be empty")
    if not math.isfinite(settings.request_timeout_seconds) or settings.request_timeout_seconds <= 0:
        raise HostConfigurationError("PHI_REQUEST_TIMEOUT_SECONDS must be finite and positive")
    return ModelConfig(
        base_url=settings.base_url,
        api_key=settings.api_key,
        default_model=settings.default_model,
        request_timeout_seconds=settings.request_timeout_seconds,
    )


async def build_host_runtime(cwd: Path) -> HostRuntime:
    """Build the production Settings, Model catalog, Sessions, and cwd lifetime once."""

    settings = Settings()
    config = model_config_from_settings(settings)
    client = httpx.AsyncClient()
    resources: RuntimeResources | None = None
    try:
        available_models = tuple(await list_available_models(config, client=client))
        resources = await build_runtime_resources(
            cwd,
            base_instructions=PHI_BASE_INSTRUCTIONS,
            approval_policy=RuleBasedApprovalPolicy(HEADLESS_MODE),
        )
    except BaseException:
        if resources is not None:
            await resources.close()
        await client.aclose()
        raise
    return HostRuntime(
        settings=settings,
        model=OpenAICompatibleModel(config, client=client),
        available_models=available_models,
        storage=SessionStorage(settings.session_dir),
        resources=resources,
        close_callback=client.aclose,
    )


class CwdRuntimeBootstrap:
    """Own and reuse the active cwd-scoped runtime resource collection."""

    def __init__(
        self,
        *,
        base_instructions: str,
        approval_policy: ApprovalPolicy,
        personal_instructions: str = "",
        global_skill_root: Path | None = None,
        global_agent_root: Path | None = None,
        global_mcp_config_path: Path | None = None,
        event_bus: EventEmitter[McpEvent] | None = None,
        default_tool_timeout_seconds: float = 30.0,
    ) -> None:
        self._base_instructions = base_instructions
        self._personal_instructions = personal_instructions
        self._global_skill_root = global_skill_root
        self._global_agent_root = global_agent_root
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
                global_agent_root=self._global_agent_root,
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
    global_agent_root: Path | None = None,
    project_agent_root: Path | None = None,
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
    agent_definitions = discover_agent_definitions(
        global_root=(global_agent_root or Path("~/.phi/agents")).expanduser(),
        project_root=project_agent_root or canonical_cwd / ".phi" / "agents",
        project_ignore_root=canonical_cwd,
    )
    agents = AgentRuntime(
        agent_definitions.definitions,
        stable_instructions=stable_instructions,
    )
    registry = build_default_registry()
    skill_tool = build_skill_tool(discovery.skills)
    if skill_tool is not None:
        registry.register(skill_tool)
    config_diagnostics: tuple[McpConfigDiagnostic, ...] = ()
    mcp = McpRuntime()
    try:
        try:
            mcp_config = await load_merged_mcp_config(
                (global_mcp_config_path or Path("~/.phi/mcp.json")).expanduser(),
                project_mcp_config_path or canonical_cwd / ".phi" / "mcp.json",
            )
        except McpConfigError as error:
            config_diagnostics = (error.diagnostic,)
        else:
            mcp = await connect_mcp_servers(
                mcp_config,
                cwd=canonical_cwd,
                registry=registry,
                events=event_bus,
            )
        agent_tools = build_agent_tools(dict(agent_definitions.definitions))
        registry.register_many(agent_tools)
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
        try:
            await agents.close()
        finally:
            await mcp.close()
        raise
    return RuntimeResources(
        cwd=canonical_cwd,
        project_instructions=project_instructions,
        skill_discovery=discovery,
        agent_definitions=agent_definitions,
        stable_instructions=stable_instructions,
        environment=environment,
        mcp=mcp,
        agents=agents,
        agent_tools=agent_tools,
        tools=registry,
        dispatcher=dispatcher,
        diagnostics=(
            *discovery.diagnostics,
            *agent_definitions.diagnostics,
            *config_diagnostics,
            *mcp.diagnostics,
        ),
    )


def _instruction_section(label: str, content: str) -> str | None:
    if not content.strip():
        return None
    ending = "" if content.endswith("\n") else "\n"
    return f"--- BEGIN {label} ---\n{content}{ending}--- END {label} ---"
