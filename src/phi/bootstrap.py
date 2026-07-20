"""为一个工作目录组装 Host 共用的运行时资源及其生命周期。"""

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
from phi.instructions import (
    PHI_BASE_INSTRUCTIONS,
    InstructionAssembly,
    InstructionSection,
    ProjectInstructions,
    load_project_instructions,
)
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
    DEFAULT_MODE,
    HEADLESS_MODE,
    ApprovalPolicy,
    ApprovalResolver,
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
    """保存为一个活动工作目录一次性组装的运行时资源。"""

    cwd: Path
    project_instructions: ProjectInstructions
    skill_discovery: SkillDiscovery
    agent_definitions: AgentDefinitionDiscovery
    instruction_assembly: InstructionAssembly
    environment: ConfinedEnvironment
    mcp: McpRuntime
    agents: AgentRuntime
    agent_tools: tuple[Tool, ...]
    tools: ToolRegistry
    dispatcher: ToolDispatcher
    diagnostics: tuple[RuntimeDiagnostic, ...]

    @property
    def stable_instructions(self) -> str:
        """返回执行与 Context 检查共用的稳定指令文本。"""

        return self.instruction_assembly.stable_instructions

    @property
    def instruction_sections(self) -> tuple[InstructionSection, ...]:
        """返回保留来源信息的稳定指令分段。"""

        return self.instruction_assembly.sections

    def invoke_skill(self, name: str) -> str:
        """通过可信用户路径选择一个已加载的 Skill。"""

        return invoke_user_skill(self.skill_discovery.skills, name)

    async def list_mcp_prompts(self, server_id: str | None = None) -> tuple[McpPrompt, ...]:
        """通过可信运行时路径列出缓存的 MCP Prompt。"""

        return await self.mcp.list_prompts(server_id)

    async def get_mcp_prompt(
        self,
        command: str,
        arguments: dict[str, str],
    ) -> McpPromptResult:
        """读取用户选择的一个 MCP Prompt，且不修改 Session。"""

        return await self.mcp.get_prompt(command, arguments)

    async def close(self) -> None:
        """依次关闭全部长生命周期的工作目录级资源。"""

        # Subagent 可能仍在使用 MCP Tool；先收拢 Agent 任务，再关闭 MCP 进程。
        try:
            await self.agents.close()
        finally:
            # 即使 Agent 清理失败也必须尝试关闭 MCP，防止遗留子进程和传输。
            await self.mcp.close()

    async def __aenter__(self) -> RuntimeResources:
        """进入异步上下文并返回已组装资源。"""

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出异步上下文时关闭 Agent 与 MCP 资源。"""

        del exc_type, exc_value, traceback
        await self.close()


class HostConfigurationError(ValueError):
    """表示可信 Settings 无法产生可用的生产 Host 运行时。"""


@dataclass
class HostRuntime:
    """保存可复用的 Model、Session 存储与工作目录级 Host 生命周期。"""

    settings: Settings
    model: Model
    available_models: tuple[ModelInfo, ...]
    storage: SessionStorage
    resources: RuntimeResources
    approval_policy: RuleBasedApprovalPolicy | None = None
    close_callback: Callable[[], Awaitable[object]] | None = None
    _closed: bool = False

    async def close(self) -> None:
        """先收拢 Agent/MCP 资源，再关闭拥有的 Model 传输。"""

        # Host 可能从多个退出路径调用 close；先置位保证清理只启动一次。
        if self._closed:
            return
        self._closed = True
        resource_error: BaseException | None = None
        # 分别保存资源错误与传输错误，确保前者不会阻止 HTTP 客户端关闭。
        try:
            await self.resources.close()
        except BaseException as error:
            resource_error = error
        try:
            if self.close_callback is not None:
                await self.close_callback()
        except BaseException:
            # 若资源清理已经失败，保留最先发生的错误；否则传播传输关闭错误。
            if resource_error is None:
                raise
        if resource_error is not None:
            raise resource_error


def model_config_from_settings(settings: Settings) -> ModelConfig:
    """从 Settings 构造可信 Model 配置，避免 Model 包反向依赖 Settings。

    Raises:
        HostConfigurationError: 凭据、端点或超时无法用于生产 Host。
    """

    # Settings 已完成环境字符串解析；此处验证生产运行时才需要的可用性条件。
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


async def build_headless_runtime(cwd: Path) -> HostRuntime:
    """使用 fail-closed 的无头 Approval Policy 构造生产运行时。"""

    return await _build_host_runtime(
        cwd,
        approval_policy=RuleBasedApprovalPolicy(HEADLESS_MODE),
    )


async def _build_host_runtime(
    cwd: Path,
    *,
    approval_policy: ApprovalPolicy,
) -> HostRuntime:
    """组装两个 Host 适配器共用的生产生命周期。"""

    # Model 配置与客户端先建立；后续任何装配失败都必须逆序清理已创建资源。
    settings = Settings()
    config = model_config_from_settings(settings)
    client = httpx.AsyncClient()
    resources: RuntimeResources | None = None
    try:
        # 可用 Model 列表与工作目录资源共享同一个 Host 启动阶段，但职责保持分离。
        available_models = tuple(await list_available_models(config, client=client))
        resources = await build_runtime_resources(
            cwd,
            base_instructions=PHI_BASE_INSTRUCTIONS,
            approval_policy=approval_policy,
        )
    except BaseException:
        # 捕获 BaseException 也覆盖任务取消，保证半组装运行时不会泄漏资源。
        if resources is not None:
            await resources.close()
        await client.aclose()
        raise
    # OpenAICompatibleModel 借用同一客户端，因此由 HostRuntime 的回调统一关闭。
    return HostRuntime(
        settings=settings,
        model=OpenAICompatibleModel(config, client=client),
        available_models=available_models,
        storage=SessionStorage(settings.session_dir),
        resources=resources,
        close_callback=client.aclose,
    )


async def build_interactive_runtime(
    cwd: Path,
    *,
    approval_resolver: ApprovalResolver,
) -> HostRuntime:
    """使用由用户交互解析 ask 的 Approval Policy 构造生产运行时。"""

    policy = RuleBasedApprovalPolicy(DEFAULT_MODE, approval_resolver)
    runtime = await _build_host_runtime(cwd, approval_policy=policy)
    runtime.approval_policy = policy
    return runtime


class CwdRuntimeBootstrap:
    """拥有并复用当前工作目录对应的运行时资源集合。"""

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
        """保存装配选项，并初始化互斥的空资源缓存。"""

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
        """返回同一工作目录的缓存资源，或按要求显式重建。"""

        # 先规范化路径，确保符号链接或相对写法不会制造重复的 cwd 生命周期。
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        # 锁覆盖“比较、关闭、重建、发布”全过程，避免并发 Host 操作交错资源代际。
        async with self._lock:
            if not rebuild and self._active is not None and self._active.cwd == canonical_cwd:
                return self._active
            # 先从缓存摘除旧资源；若关闭或重建失败，调用方不会拿到已关闭对象。
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
            # 只有完整装配成功后才原子地发布为当前活动资源。
            self._active = resources
            return resources

    async def close(self) -> None:
        """关闭当前工作目录生命周期并清空缓存。"""

        async with self._lock:
            # 先清空引用再 await，重入式观察不会取得正在关闭的资源。
            active = self._active
            self._active = None
            if active is not None:
                await active.close()

    async def __aenter__(self) -> CwdRuntimeBootstrap:
        """进入异步上下文并返回 Bootstrap 管理器。"""

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出异步上下文时关闭缓存的工作目录资源。"""

        del exc_type, exc_value, traceback
        await self.close()


def assemble_stable_instructions(
    *,
    base_instructions: str,
    personal_instructions: str,
    project_instructions: ProjectInstructions,
    skill_discovery: SkillDiscovery,
) -> str:
    """组合 Context 检查与执行共用的唯一稳定指令前缀。"""

    # 文本渲染委托给结构化 assembly，避免检查界面再从分隔符反向解析来源。
    assembly = assemble_instruction_assembly(
        base_instructions=base_instructions,
        personal_instructions=personal_instructions,
        project_instructions=project_instructions,
        skill_discovery=skill_discovery,
    )
    return assembly.stable_instructions


def assemble_instruction_assembly(
    *,
    base_instructions: str,
    personal_instructions: str,
    project_instructions: ProjectInstructions,
    skill_discovery: SkillDiscovery,
) -> InstructionAssembly:
    """在不解析渲染后 system prompt 的前提下保留可信指令来源。"""

    # 顺序就是最终 system prompt 的权威优先序；内容为空的可选分段稍后过滤。
    candidates = (
        InstructionSection(
            id="phi-base",
            delimiter_label="Phi base instructions",
            origin="Phi base",
            source="Phi built-in instructions",
            content=base_instructions,
        ),
        InstructionSection(
            id="personal",
            delimiter_label="Personal instructions",
            origin="Personal",
            source="Host-configured personal instructions",
            content=personal_instructions,
        ),
        InstructionSection(
            id="project",
            delimiter_label="Project instructions",
            origin="Project",
            source=(
                str(project_instructions.source_path)
                if project_instructions.source_path is not None
                else "No project instruction file"
            ),
            content=project_instructions.content,
        ),
        InstructionSection(
            id="model-skills",
            delimiter_label="Model-invokable Skills",
            origin="Model-invocable Skills",
            source="Discovered Model-invocable Skill menu",
            content=render_model_skill_menu(skill_discovery.skills),
        ),
    )
    # 不保留空分段，使执行文本与检查界面看到的来源集合严格一致。
    return InstructionAssembly(tuple(section for section in candidates if section.content.strip()))


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
    """为工作目录组装 Project Instructions、扩展能力与 Tool 执行边界。

    装配顺序先建立无副作用的本地资源，再连接 MCP，最后构造 Tool Dispatcher；任何
    中途失败都会关闭已经启动的 Agent/MCP 长生命周期资源。
    """

    # Environment 首先规范化 cwd，并为内置文件与 Shell Tool 提供真实执行边界。
    environment = ConfinedEnvironment(cwd)
    canonical_cwd = environment.root
    project_instructions = load_project_instructions(canonical_cwd)
    # Project Skill 覆盖全局同名定义；单个无效定义只进入诊断而不阻断批量加载。
    discovery = discover_skills(
        global_root=(global_skill_root or Path("~/.phi/skills")).expanduser(),
        project_root=project_skill_root or canonical_cwd / ".phi" / "skills",
        project_ignore_root=canonical_cwd,
    )
    # 稳定指令只组装一次，随后同时交给普通 Agent 与 Subagent 使用。
    instruction_assembly = assemble_instruction_assembly(
        base_instructions=base_instructions,
        personal_instructions=personal_instructions,
        project_instructions=project_instructions,
        skill_discovery=discovery,
    )
    stable_instructions = instruction_assembly.stable_instructions
    # Agent Definition 采用同样的全局/项目覆盖模型，但不继承父 Agent 的 Context。
    agent_definitions = discover_agent_definitions(
        global_root=(global_agent_root or Path("~/.phi/agents")).expanduser(),
        project_root=project_agent_root or canonical_cwd / ".phi" / "agents",
        project_ignore_root=canonical_cwd,
    )
    agents = AgentRuntime(
        agent_definitions.definitions,
        stable_instructions=stable_instructions,
    )
    # 所有能力最终汇入同一个 Tool Registry，避免 Host 或 Subagent 建立隐藏执行环。
    registry = build_default_registry()
    skill_tool = build_skill_tool(discovery.skills)
    if skill_tool is not None:
        registry.register(skill_tool)
    config_diagnostics: tuple[McpConfigDiagnostic, ...] = ()
    mcp = McpRuntime()
    try:
        try:
            # MCP 配置损坏按诊断降级：本地 Tools 仍可使用，MCP 则 fail-closed 为禁用。
            mcp_config = await load_merged_mcp_config(
                (global_mcp_config_path or Path("~/.phi/mcp.json")).expanduser(),
                project_mcp_config_path or canonical_cwd / ".phi" / "mcp.json",
            )
        except McpConfigError as error:
            config_diagnostics = (error.diagnostic,)
        else:
            # 每个 MCP server 独立连接；成功发现的 Tool 直接注册到公共 Registry。
            mcp = await connect_mcp_servers(
                mcp_config,
                cwd=canonical_cwd,
                registry=registry,
                events=event_bus,
            )
        # Delegation Tool 只组合现有 Session/Run 服务，其定义在 MCP 后注册并接受冲突校验。
        agent_tools = build_agent_tools(dict(agent_definitions.definitions))
        registry.register_many(agent_tools)
        # Dispatcher 是 Model 提议到真实执行之间的唯一授权、校验和超时边界。
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
        # 捕获取消与普通异常，并以 Agent→MCP 顺序清理部分组装的外部资源。
        try:
            await agents.close()
        finally:
            await mcp.close()
        raise
    # 诊断按来源稳定拼接，供 Host 按同一顺序展示启动时的非致命问题。
    return RuntimeResources(
        cwd=canonical_cwd,
        project_instructions=project_instructions,
        skill_discovery=discovery,
        agent_definitions=agent_definitions,
        instruction_assembly=instruction_assembly,
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
