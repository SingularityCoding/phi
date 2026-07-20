"""按依赖顺序诊断 Phi 配置、工作目录资源与外部连接就绪状态。"""

from __future__ import annotations

import math
import os
import platform
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx

from phi.agents import discover_agent_definitions
from phi.bootstrap import HostConfigurationError, model_config_from_settings
from phi.harness import effective_input_limit
from phi.instructions import ProjectInstructionsError, load_project_instructions
from phi.mcp import (
    McpConfig,
    McpConfigError,
    McpDiagnostic,
    connect_mcp_servers,
    load_merged_mcp_config,
)
from phi.model import (
    ModelConfig,
    ModelInfo,
    ModelRequest,
    OpenAICompatibleModel,
)
from phi.sessions import redact_text
from phi.settings import Settings
from phi.skills import discover_skills
from phi.tools import build_default_registry

DOCTOR_SCHEMA_VERSION = 1
type DoctorMode = Literal["standard", "deep"]
type SettingsFactory = Callable[[], Settings]
type ModelDiscovery = Callable[[ModelConfig], Awaitable[list[ModelInfo]]]
type ModelProbe = Callable[[ModelConfig], Awaitable[None]]
type McpProbe = Callable[[McpConfig, Path], Awaitable[McpProbeResult]]


class DoctorStatus(StrEnum):
    """枚举单项诊断的通过、警告、失败与依赖跳过状态。"""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


class DoctorSection(StrEnum):
    """枚举报告中保持稳定顺序的诊断分层。"""

    CONFIGURATION = "configuration"
    WORKSPACE = "workspace"
    MODEL_GATEWAY = "model_gateway"
    DEEP_CHECKS = "deep_checks"


@dataclass(frozen=True)
class DoctorCheck:
    """描述一个可序列化、可脱敏的原子诊断结果。"""

    id: str
    section: DoctorSection
    title: str
    status: DoctorStatus
    summary: str
    details: tuple[str, ...] = ()
    remediation: str | None = None
    duration_ms: int | None = None
    blocked_by: tuple[str, ...] = ()

    def to_document(self) -> dict[str, object]:
        """转换为版本化 JSON 报告使用的普通文档结构。"""

        return {
            "id": self.id,
            "section": self.section.value,
            "title": self.title,
            "status": self.status.value.lower(),
            "summary": self.summary,
            "details": list(self.details),
            "remediation": self.remediation,
            "duration_ms": self.duration_ms,
            "blocked_by": list(self.blocked_by),
        }


@dataclass(frozen=True)
class DoctorReport:
    """汇总一次 standard 或 deep doctor 执行的完整结果。"""

    mode: DoctorMode
    checks: tuple[DoctorCheck, ...]
    environment: tuple[tuple[str, str], ...]
    duration_ms: int

    @property
    def healthy(self) -> bool:
        """仅当没有必需检查失败时返回真；WARN 不影响退出成功。"""

        return all(check.status is not DoctorStatus.FAIL for check in self.checks)

    @property
    def counts(self) -> Mapping[DoctorStatus, int]:
        """按状态统计检查数量，并保留所有枚举键。"""

        return {
            status: sum(check.status is status for check in self.checks) for status in DoctorStatus
        }

    def to_document(self) -> dict[str, object]:
        """转换为含 schema 版本、环境与汇总计数的 JSON 文档。"""

        counts = self.counts
        return {
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "mode": self.mode,
            "healthy": self.healthy,
            "duration_ms": self.duration_ms,
            "environment": dict(self.environment),
            "summary": {status.value.lower(): counts[status] for status in DoctorStatus},
            "checks": [check.to_document() for check in self.checks],
        }


@dataclass(frozen=True)
class McpProbeResult:
    """记录 deep MCP 探测期间的启用数、连接数与诊断。"""

    enabled_count: int
    connected: tuple[tuple[str, int], ...]
    diagnostics: tuple[McpDiagnostic, ...]


@dataclass(frozen=True)
class DoctorDependencies:
    """集中注入 doctor 的配置与外部探测依赖，便于确定性测试。"""

    settings_factory: SettingsFactory
    model_discovery: ModelDiscovery
    model_probe: ModelProbe
    mcp_probe: McpProbe


@dataclass(frozen=True)
class _WorkspaceDiagnosis:
    """携带工作目录检查及后续 deep MCP 检查所需的中间值。"""

    checks: tuple[DoctorCheck, ...]
    cwd: Path | None
    mcp_config: McpConfig | None


async def run_doctor(
    cwd: Path,
    *,
    deep: bool,
    dependencies: DoctorDependencies,
) -> DoctorReport:
    """诊断一个工作目录能否使用默认 Model 启动普通 Run。

    standard 模式只检查静态资源与 Model 发现；deep 模式额外启动后关闭 MCP server，
    并发送一个不会执行 Tool 的最小流式 Model 请求。
    """

    started = perf_counter()
    checks: list[DoctorCheck] = []
    # Settings 是多数后续检查的根依赖；失败后仍生成完整的 SKIP 链，而非提前退出。
    settings, settings_check = _load_settings(dependencies.settings_factory)
    checks.append(settings_check)

    config: ModelConfig | None = None
    if settings is None:
        # 保持检查名称和顺序稳定，机器消费者无需根据早期失败猜测缺失项。
        checks.extend(
            (
                _skipped_check(
                    "configuration.model",
                    DoctorSection.CONFIGURATION,
                    "Model settings",
                    ("configuration.settings",),
                ),
                _skipped_check(
                    "configuration.default-model",
                    DoctorSection.CONFIGURATION,
                    "Default Model",
                    ("configuration.settings",),
                ),
            )
        )
    else:
        config, model_check = _model_configuration(settings)
        checks.append(model_check)
        checks.append(_configured_default_model(settings))

    # 工作目录资源与 Model Gateway 大多相互独立，即使配置失败也尽可能继续诊断。
    workspace = await _inspect_workspace(cwd, settings)
    checks.extend(workspace.checks)

    models: list[ModelInfo] | None = None
    if config is None:
        discovery_check = _skipped_check(
            "model.discovery",
            DoctorSection.MODEL_GATEWAY,
            "Model discovery",
            ("configuration.model",),
        )
    else:
        models, discovery_check = await _discover_models(config, settings, dependencies)
    checks.append(discovery_check)

    # Model 发现成功后再验证默认项和 Context 容量，形成显式依赖链。
    default_model, default_check = _available_default_model(settings, models, discovery_check)
    checks.append(default_check)
    checks.append(_context_limit_check(settings, default_model, default_check))

    if deep:
        # 深度探测置于最后，且彼此独立：MCP 警告不应阻止 Model 连通性检查。
        checks.append(await _deep_mcp_check(workspace, settings, dependencies))
        checks.append(await _deep_model_check(config, settings, default_check, dependencies))

    return DoctorReport(
        mode="deep" if deep else "standard",
        checks=tuple(checks),
        environment=_environment_facts(cwd, settings),
        duration_ms=_elapsed_ms(started),
    )


def _load_settings(factory: SettingsFactory) -> tuple[Settings | None, DoctorCheck]:
    """加载环境 Settings，并把解析失败转换为首项诊断。"""

    started = perf_counter()
    try:
        settings = factory()
    except Exception as error:
        # 所有异常文案在进入报告前统一脱敏，避免 Pydantic 错误回显凭据。
        detail = _safe_text(str(error), None) or type(error).__name__
        return None, DoctorCheck(
            id="configuration.settings",
            section=DoctorSection.CONFIGURATION,
            title="Settings",
            status=DoctorStatus.FAIL,
            summary="PHI_* settings could not be loaded",
            details=(detail,),
            remediation="Correct invalid PHI_* values in the environment or .env.",
            duration_ms=_elapsed_ms(started),
        )
    return settings, DoctorCheck(
        id="configuration.settings",
        section=DoctorSection.CONFIGURATION,
        title="Settings",
        status=DoctorStatus.PASS,
        summary="PHI_* settings loaded",
        duration_ms=_elapsed_ms(started),
    )


def _model_configuration(settings: Settings) -> tuple[ModelConfig | None, DoctorCheck]:
    """验证 Settings 能否构造安全可用的 ModelConfig。"""

    started = perf_counter()
    try:
        config = model_config_from_settings(settings)
        # bootstrap 验证非空配置；doctor 额外要求绝对 HTTP(S) URL 以便给出精确建议。
        _validate_base_url(config.base_url)
    except Exception as error:
        detail = _safe_text(str(error), settings) or type(error).__name__
        return None, DoctorCheck(
            id="configuration.model",
            section=DoctorSection.CONFIGURATION,
            title="Model settings",
            status=DoctorStatus.FAIL,
            summary="Model settings are invalid",
            details=(detail,),
            remediation=_model_settings_remediation(detail),
            duration_ms=_elapsed_ms(started),
        )
    return config, DoctorCheck(
        id="configuration.model",
        section=DoctorSection.CONFIGURATION,
        title="Model settings",
        status=DoctorStatus.PASS,
        summary=(
            f"{_safe_text(_display_url(config.base_url), settings)} · "
            f"credential configured · timeout {_number(config.request_timeout_seconds)}s"
        ),
        duration_ms=_elapsed_ms(started),
    )


def _configured_default_model(settings: Settings) -> DoctorCheck:
    """检查 PHI_DEFAULT_MODEL 是否至少配置了非空标识。"""

    model_id = settings.default_model.strip()
    if not model_id:
        return DoctorCheck(
            id="configuration.default-model",
            section=DoctorSection.CONFIGURATION,
            title="Default Model",
            status=DoctorStatus.FAIL,
            summary="PHI_DEFAULT_MODEL is not configured",
            remediation="Set PHI_DEFAULT_MODEL in the environment or .env.",
        )
    return DoctorCheck(
        id="configuration.default-model",
        section=DoctorSection.CONFIGURATION,
        title="Default Model",
        status=DoctorStatus.PASS,
        summary=_safe_text(model_id, settings),
    )


async def _inspect_workspace(cwd: Path, settings: Settings | None) -> _WorkspaceDiagnosis:
    """按固定顺序检查 cwd 及依赖它的本地资源。"""

    started = perf_counter()
    try:
        # strict 解析同时验证存在性并消除符号链接，后续路径展示与命令检查共享该根。
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        if not canonical_cwd.is_dir():
            raise NotADirectoryError(str(canonical_cwd))
    except (OSError, RuntimeError) as error:
        cwd_check = DoctorCheck(
            id="workspace.cwd",
            section=DoctorSection.WORKSPACE,
            title="Working directory",
            status=DoctorStatus.FAIL,
            summary="Working directory is unavailable",
            details=(_safe_text(str(error), settings) or type(error).__name__,),
            remediation="Run Phi from an existing, readable project directory.",
            duration_ms=_elapsed_ms(started),
        )
        # cwd 是所有工作目录资源的共同前置条件；失败时显式标记下游 SKIP。
        blocked = ("workspace.cwd",)
        return _WorkspaceDiagnosis(
            checks=(
                cwd_check,
                _skipped_check(
                    "workspace.session-storage",
                    DoctorSection.WORKSPACE,
                    "Session storage",
                    blocked,
                ),
                _skipped_check(
                    "workspace.instructions",
                    DoctorSection.WORKSPACE,
                    "Project Instructions",
                    blocked,
                ),
                _skipped_check(
                    "workspace.skills",
                    DoctorSection.WORKSPACE,
                    "Skills",
                    blocked,
                ),
                _skipped_check(
                    "workspace.agent-definitions",
                    DoctorSection.WORKSPACE,
                    "Agent Definitions",
                    blocked,
                ),
                _skipped_check(
                    "workspace.mcp-configuration",
                    DoctorSection.WORKSPACE,
                    "MCP configuration",
                    blocked,
                ),
            ),
            cwd=None,
            mcp_config=None,
        )

    # 检查追加顺序就是人类与 JSON 报告中的稳定顺序，不按状态重新排序。
    checks = [
        DoctorCheck(
            id="workspace.cwd",
            section=DoctorSection.WORKSPACE,
            title="Working directory",
            status=DoctorStatus.PASS,
            summary=_display_path(canonical_cwd, canonical_cwd),
            duration_ms=_elapsed_ms(started),
        )
    ]
    checks.append(_session_storage_check(settings, canonical_cwd))
    checks.append(_instructions_check(canonical_cwd, settings))
    checks.append(_skills_check(canonical_cwd, settings))
    checks.append(_agent_definitions_check(canonical_cwd, settings))
    mcp_check, mcp_config = await _mcp_configuration_check(canonical_cwd, settings)
    checks.append(mcp_check)
    return _WorkspaceDiagnosis(tuple(checks), canonical_cwd, mcp_config)


def _session_storage_check(settings: Settings | None, cwd: Path) -> DoctorCheck:
    """检查 Session 存储目录现有权限或未来可创建性。"""

    started = perf_counter()
    if settings is None:
        return _skipped_check(
            "workspace.session-storage",
            DoctorSection.WORKSPACE,
            "Session storage",
            ("configuration.settings",),
        )

    path = settings.session_dir.expanduser()
    display = _safe_text(_display_path(path, cwd), settings)
    try:
        if path.exists():
            # 现有目录需要读、写、遍历权限，才能同时读取和原子追加 Session 文件。
            if not path.is_dir():
                raise NotADirectoryError(f"{display} is not a directory")
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                raise PermissionError(f"{display} is not readable and writable")
            summary = f"{display} · readable and writable"
        else:
            # 不创建目录；只向上寻找最近存在父目录，保持 standard doctor 无副作用。
            parent = path.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
                raise PermissionError(f"{display} cannot be created")
            summary = f"{display} · will be created"
    except OSError as error:
        return DoctorCheck(
            id="workspace.session-storage",
            section=DoctorSection.WORKSPACE,
            title="Session storage",
            status=DoctorStatus.FAIL,
            summary="Session storage is unavailable",
            details=(_safe_text(str(error), settings) or type(error).__name__,),
            remediation="Set PHI_SESSION_DIR to a readable and writable directory.",
            duration_ms=_elapsed_ms(started),
        )
    return DoctorCheck(
        id="workspace.session-storage",
        section=DoctorSection.WORKSPACE,
        title="Session storage",
        status=DoctorStatus.PASS,
        summary=summary,
        duration_ms=_elapsed_ms(started),
    )


def _instructions_check(cwd: Path, settings: Settings | None) -> DoctorCheck:
    """按正常启动规则读取可选 Project Instructions。"""

    started = perf_counter()
    try:
        instructions = load_project_instructions(cwd)
    except ProjectInstructionsError as error:
        return DoctorCheck(
            id="workspace.instructions",
            section=DoctorSection.WORKSPACE,
            title="Project Instructions",
            status=DoctorStatus.FAIL,
            summary="Project Instructions could not be loaded",
            details=(_safe_text(str(error), settings),),
            remediation="Correct the selected AGENTS.md or CLAUDE.md file.",
            duration_ms=_elapsed_ms(started),
        )
    # 缺少指令文件是合法的可选状态；只有选中文件无法读取才是 FAIL。
    summary = (
        "No project file · optional"
        if instructions.source_path is None
        else f"{_safe_text(_display_path(instructions.source_path, cwd), settings)} loaded"
    )
    return DoctorCheck(
        id="workspace.instructions",
        section=DoctorSection.WORKSPACE,
        title="Project Instructions",
        status=DoctorStatus.PASS,
        summary=summary,
        duration_ms=_elapsed_ms(started),
    )


def _skills_check(cwd: Path, settings: Settings | None) -> DoctorCheck:
    """发现全局与项目 Skill，并把单项无效定义汇总为 WARN。"""

    started = perf_counter()
    discovery = discover_skills(
        global_root=Path("~/.phi/skills").expanduser(),
        project_root=cwd / ".phi" / "skills",
        project_ignore_root=cwd,
    )
    # 复用真实启动发现器，确保 doctor 不维护第二套覆盖和校验规则。
    diagnostics = tuple(_safe_text(str(item), settings) for item in discovery.diagnostics)
    invalid = len(diagnostics)
    return DoctorCheck(
        id="workspace.skills",
        section=DoctorSection.WORKSPACE,
        title="Skills",
        status=DoctorStatus.WARN if diagnostics else DoctorStatus.PASS,
        summary=f"{len(discovery.skills)} loaded" + (f" · {invalid} invalid" if invalid else ""),
        details=diagnostics,
        remediation=(
            "Correct or remove invalid Skill definitions, then rerun doctor."
            if diagnostics
            else None
        ),
        duration_ms=_elapsed_ms(started),
    )


def _agent_definitions_check(cwd: Path, settings: Settings | None) -> DoctorCheck:
    """发现 Agent Definition，并把容错批量诊断汇总为 WARN。"""

    started = perf_counter()
    discovery = discover_agent_definitions(
        global_root=Path("~/.phi/agents").expanduser(),
        project_root=cwd / ".phi" / "agents",
        project_ignore_root=cwd,
    )
    diagnostics = tuple(_safe_text(str(item), settings) for item in discovery.diagnostics)
    invalid = len(diagnostics)
    return DoctorCheck(
        id="workspace.agent-definitions",
        section=DoctorSection.WORKSPACE,
        title="Agent Definitions",
        status=DoctorStatus.WARN if diagnostics else DoctorStatus.PASS,
        summary=f"{len(discovery.definitions)} loaded"
        + (f" · {invalid} invalid" if invalid else ""),
        details=diagnostics,
        remediation=(
            "Correct or remove invalid Agent Definitions, then rerun doctor."
            if diagnostics
            else None
        ),
        duration_ms=_elapsed_ms(started),
    )


async def _mcp_configuration_check(
    cwd: Path,
    settings: Settings | None,
) -> tuple[DoctorCheck, McpConfig | None]:
    """合并 MCP 配置，并静态验证已启用命令是否可解析。"""

    started = perf_counter()
    try:
        config = await load_merged_mcp_config(
            Path("~/.phi/mcp.json").expanduser(),
            cwd / ".phi" / "mcp.json",
        )
    except McpConfigError as error:
        # MCP 是可选扩展；配置损坏使它 fail-closed 为禁用，但普通 Run 仍可能可用。
        return DoctorCheck(
            id="workspace.mcp-configuration",
            section=DoctorSection.WORKSPACE,
            title="MCP configuration",
            status=DoctorStatus.WARN,
            summary="MCP configuration is invalid · MCP disabled",
            details=(_safe_text(str(error), settings),),
            remediation="Correct the reported MCP JSON configuration.",
            duration_ms=_elapsed_ms(started),
        ), None

    enabled = {server_id: item for server_id, item in config.servers.items() if item.enabled}
    # standard 模式只检查命令存在性，不启动进程、不初始化 MCP Session。
    missing = tuple(
        f"MCP server {server_id!r}: command not found: {item.command}"
        for server_id, item in sorted(enabled.items())
        if not _command_available(item.command, cwd)
    )
    enabled_count = len(enabled)
    disabled_count = len(config.servers) - enabled_count
    return DoctorCheck(
        id="workspace.mcp-configuration",
        section=DoctorSection.WORKSPACE,
        title="MCP configuration",
        status=DoctorStatus.WARN if missing else DoctorStatus.PASS,
        summary=f"{enabled_count} enabled · {disabled_count} disabled",
        details=tuple(_safe_text(item, settings) for item in missing),
        remediation=(
            "Install missing MCP commands or disable those server definitions." if missing else None
        ),
        duration_ms=_elapsed_ms(started),
    ), config


async def _discover_models(
    config: ModelConfig,
    settings: Settings | None,
    dependencies: DoctorDependencies,
) -> tuple[list[ModelInfo] | None, DoctorCheck]:
    """调用注入的 Model 发现边界，并规范化网络或协议失败。"""

    started = perf_counter()
    try:
        models = await dependencies.model_discovery(config)
    except Exception as error:
        # 保留类型供 remediation 判断，同时仅把脱敏后的文本写入报告。
        detail = _safe_text(str(error), settings) or type(error).__name__
        return None, DoctorCheck(
            id="model.discovery",
            section=DoctorSection.MODEL_GATEWAY,
            title="Model discovery",
            status=DoctorStatus.FAIL,
            summary="Could not list Models",
            details=(detail,),
            remediation=_model_error_remediation(error),
            duration_ms=_elapsed_ms(started),
        )
    return models, DoctorCheck(
        id="model.discovery",
        section=DoctorSection.MODEL_GATEWAY,
        title="Model discovery",
        status=DoctorStatus.PASS,
        summary=f"{len(models)} available",
        duration_ms=_elapsed_ms(started),
    )


def _available_default_model(
    settings: Settings | None,
    models: list[ModelInfo] | None,
    discovery_check: DoctorCheck,
) -> tuple[ModelInfo | None, DoctorCheck]:
    """在发现结果中查找配置的默认 Model，并编码阻塞依赖。"""

    if settings is None:
        return None, _skipped_check(
            "model.default-model",
            DoctorSection.MODEL_GATEWAY,
            "Default Model available",
            ("configuration.settings",),
        )
    model_id = settings.default_model.strip()
    if not model_id:
        return None, _skipped_check(
            "model.default-model",
            DoctorSection.MODEL_GATEWAY,
            "Default Model available",
            ("configuration.default-model",),
        )
    if discovery_check.status is not DoctorStatus.PASS or models is None:
        return None, _skipped_check(
            "model.default-model",
            DoctorSection.MODEL_GATEWAY,
            "Default Model available",
            ("model.discovery",),
        )
    selected = next((model for model in models if model.id == model_id), None)
    if selected is None:
        # 仅展示少量可选项，避免庞大或不可信注册表撑爆诊断输出。
        available = ", ".join(_safe_text(model.id, settings) for model in models[:5])
        details = (f"Available Models include: {available}",) if available else ()
        return None, DoctorCheck(
            id="model.default-model",
            section=DoctorSection.MODEL_GATEWAY,
            title="Default Model available",
            status=DoctorStatus.FAIL,
            summary=f"{_safe_text(model_id, settings)} is not available",
            details=details,
            remediation="Select an available Model and update PHI_DEFAULT_MODEL.",
        )
    return selected, DoctorCheck(
        id="model.default-model",
        section=DoctorSection.MODEL_GATEWAY,
        title="Default Model available",
        status=DoctorStatus.PASS,
        summary=_safe_text(model_id, settings),
    )


def _context_limit_check(
    settings: Settings | None,
    model: ModelInfo | None,
    default_check: DoctorCheck,
) -> DoctorCheck:
    """检查默认 Model 是否具有可用于 Context 预算的有效输入限制。"""

    if settings is None or model is None:
        return _skipped_check(
            "model.context-limit",
            DoctorSection.MODEL_GATEWAY,
            "Context limit",
            (default_check.id,),
        )
    # 使用 Harness 的同一策略：本地配置只能缩小提供方限制，不能将其放大。
    input_limit = effective_input_limit(model, settings.compaction)
    output = "unknown" if model.max_output_tokens is None else str(model.max_output_tokens)
    if input_limit is None:
        # 容量未知不妨碍兼容模式运行，但必须 WARN，不能编造默认 Context 窗口。
        return DoctorCheck(
            id="model.context-limit",
            section=DoctorSection.MODEL_GATEWAY,
            title="Context limit",
            status=DoctorStatus.WARN,
            summary=f"input unknown · output {output}",
            remediation=(
                "Set PHI_COMPACTION_MAX_INPUT_TOKENS when the Proxy does not advertise an "
                "input limit."
            ),
        )
    return DoctorCheck(
        id="model.context-limit",
        section=DoctorSection.MODEL_GATEWAY,
        title="Context limit",
        status=DoctorStatus.PASS,
        summary=f"input {input_limit} · output {output}",
    )


async def _deep_mcp_check(
    workspace: _WorkspaceDiagnosis,
    settings: Settings | None,
    dependencies: DoctorDependencies,
) -> DoctorCheck:
    """启动并关闭已启用 MCP server，报告实际初始化结果。"""

    # 深度探测只在静态依赖可用时执行，避免用二次异常遮蔽根因。
    if workspace.cwd is None:
        return _skipped_check(
            "deep.mcp-servers",
            DoctorSection.DEEP_CHECKS,
            "MCP servers",
            ("workspace.cwd",),
        )
    if workspace.mcp_config is None:
        return _skipped_check(
            "deep.mcp-servers",
            DoctorSection.DEEP_CHECKS,
            "MCP servers",
            ("workspace.mcp-configuration",),
        )
    started = perf_counter()
    try:
        result = await dependencies.mcp_probe(workspace.mcp_config, workspace.cwd)
    except Exception as error:
        return DoctorCheck(
            id="deep.mcp-servers",
            section=DoctorSection.DEEP_CHECKS,
            title="MCP servers",
            status=DoctorStatus.WARN,
            summary="MCP startup probe failed",
            details=(_safe_text(str(error), settings) or type(error).__name__,),
            remediation="Inspect the enabled MCP commands and rerun doctor --deep.",
            duration_ms=_elapsed_ms(started),
        )
    connected_count = len(result.connected)
    # MCP 是可选扩展：部分或全部 server 失败均为 WARN，不改变必需 Host 就绪性。
    if result.diagnostics:
        return DoctorCheck(
            id="deep.mcp-servers",
            section=DoctorSection.DEEP_CHECKS,
            title="MCP servers",
            status=DoctorStatus.WARN,
            summary=f"{connected_count} of {result.enabled_count} connected",
            details=tuple(_safe_text(str(item), settings) for item in result.diagnostics),
            remediation="Correct failed MCP server configuration or disable it.",
            duration_ms=_elapsed_ms(started),
        )
    summary = (
        "No enabled servers"
        if result.enabled_count == 0
        else f"{connected_count} of {result.enabled_count} connected"
    )
    details = tuple(
        _safe_text(f"{server_id}: {count} Tools", settings) for server_id, count in result.connected
    )
    return DoctorCheck(
        id="deep.mcp-servers",
        section=DoctorSection.DEEP_CHECKS,
        title="MCP servers",
        status=DoctorStatus.PASS,
        summary=summary,
        details=details,
        duration_ms=_elapsed_ms(started),
    )


async def _deep_model_check(
    config: ModelConfig | None,
    settings: Settings | None,
    default_check: DoctorCheck,
    dependencies: DoctorDependencies,
) -> DoctorCheck:
    """向已验证的默认 Model 发送一次最小流式连通性请求。"""

    # 只有配置合法且默认 Model 确认可用时，网络请求才具有可解释性。
    if config is None or default_check.status is not DoctorStatus.PASS:
        return _skipped_check(
            "deep.model-request",
            DoctorSection.DEEP_CHECKS,
            "Streaming Model request",
            (default_check.id,),
        )
    started = perf_counter()
    try:
        await dependencies.model_probe(config)
    except Exception as error:
        return DoctorCheck(
            id="deep.model-request",
            section=DoctorSection.DEEP_CHECKS,
            title="Streaming Model request",
            status=DoctorStatus.FAIL,
            summary="Default Model request failed",
            details=(_safe_text(str(error), settings) or type(error).__name__,),
            remediation=_model_error_remediation(error),
            duration_ms=_elapsed_ms(started),
        )
    return DoctorCheck(
        id="deep.model-request",
        section=DoctorSection.DEEP_CHECKS,
        title="Streaming Model request",
        status=DoctorStatus.PASS,
        summary="OpenAI-compatible stream completed",
        duration_ms=_elapsed_ms(started),
    )


async def probe_default_model(config: ModelConfig) -> None:
    """发送一次有界流式请求，不创建 Session，也不执行 Tool。"""

    # 提供一个不可执行的虚拟 Tool schema，用最小代价验证真实启动所需的工具协议形状。
    request = ModelRequest(
        model=config.default_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "This is a connectivity diagnostic. Reply briefly and do not call Tools."
                ),
            },
            {"role": "user", "content": "Reply with OK."},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "phi_doctor_probe",
                    "description": "A diagnostic Tool that must not be called.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ],
        temperature=0,
        max_tokens=16,
    )
    # 完整消费至 [DONE] 才能验证 SSE 完整性；异步上下文保证 HTTP 客户端关闭。
    async with OpenAICompatibleModel(config) as model:
        async for _event in model.request_stream(request):
            pass


async def probe_mcp_servers(config: McpConfig, cwd: Path) -> McpProbeResult:
    """启动已启用 MCP server、检查结果并关闭全部进程。"""

    enabled_count = sum(item.enabled for item in config.servers.values())
    # 使用临时 Registry 触发真实 Tool 发现与冲突校验，但不污染 Host 运行时。
    runtime = await connect_mcp_servers(config, cwd=cwd, registry=build_default_registry())
    try:
        return McpProbeResult(
            enabled_count=enabled_count,
            connected=runtime.server_tool_counts,
            diagnostics=runtime.diagnostics,
        )
    finally:
        # 无论结果读取或返回构造是否失败，都不能让诊断进程遗留 MCP 子进程。
        await runtime.close()


def _skipped_check(
    check_id: str,
    section: DoctorSection,
    title: str,
    blocked_by: tuple[str, ...],
) -> DoctorCheck:
    """构造一个明确列出阻塞依赖的稳定 SKIP 检查。"""

    return DoctorCheck(
        id=check_id,
        section=section,
        title=title,
        status=DoctorStatus.SKIP,
        summary=f"blocked by {', '.join(blocked_by)}",
        blocked_by=blocked_by,
    )


def _validate_base_url(base_url: str) -> None:
    """要求 Model Base URL 是带主机的绝对 HTTP(S) URL。"""

    try:
        parsed = httpx.URL(base_url)
    except httpx.InvalidURL:
        raise HostConfigurationError("PHI_BASE_URL must be an absolute HTTP(S) URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise HostConfigurationError("PHI_BASE_URL must be an absolute HTTP(S) URL")


def _model_settings_remediation(detail: str) -> str:
    """根据已脱敏的配置错误详情选择具体修复建议。"""

    if "PHI_API_KEY" in detail:
        return "Set PHI_API_KEY in the environment or .env."
    if "PHI_BASE_URL" in detail:
        return "Set PHI_BASE_URL to the OpenAI-compatible Proxy /v1 endpoint."
    if "PHI_REQUEST_TIMEOUT_SECONDS" in detail:
        return "Set PHI_REQUEST_TIMEOUT_SECONDS to a finite positive number."
    return "Correct the reported PHI_* Model setting."


def _model_error_remediation(error: Exception) -> str:
    """根据类型化状态码或超时信号选择 Model 连接修复建议。"""

    # 优先读取类型化属性；仅对无法分类的兼容异常使用保守的文本超时判断。
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403}:
        return "Verify PHI_API_KEY and that it can access the configured Proxy."
    if "timed out" in str(error).casefold():
        return "Check Proxy connectivity and PHI_REQUEST_TIMEOUT_SECONDS."
    return "Check Proxy connectivity, credentials, and OpenAI-compatible protocol support."


def _safe_text(value: str, settings: Settings | None) -> str:
    """移除 API key 及通用敏感片段后，返回可安全展示的文本。"""

    # 先精确替换当前 Settings 中的 Secret，再应用 Session Trace 共用的通用脱敏器。
    if settings is not None:
        secret = settings.api_key.get_secret_value()
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return redact_text(value) or ""


def _display_url(value: str) -> str:
    """规范化 URL 供诊断展示，同时正确包裹 IPv6 主机。"""

    parsed = httpx.URL(value)
    host = parsed.host or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{port}{path}"


def _display_path(path: Path, cwd: Path) -> str:
    """优先把路径显示为 cwd 相对形式，其次缩写为 home 相对形式。"""

    path = path.expanduser()
    try:
        return str(path.relative_to(cwd)) or "."
    except ValueError:
        pass
    home = Path.home()
    try:
        return str(Path("~") / path.relative_to(home))
    except ValueError:
        return str(path)


def _command_available(command: str, cwd: Path) -> bool:
    """判断 MCP 命令能否按路径或 PATH 规则找到并执行。"""

    candidate = Path(command).expanduser()
    # 含目录部分的命令不得回退到 PATH；相对路径严格相对于被诊断 cwd。
    if candidate.is_absolute() or candidate.parent != Path("."):
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(command) is not None


def _environment_facts(
    cwd: Path,
    settings: Settings | None,
) -> tuple[tuple[str, str], ...]:
    """按固定键顺序收集已脱敏的只读运行环境事实。"""

    # 源码环境可能尚未安装分发元数据；这不是运行时就绪性失败。
    try:
        phi_version = version("phi")
    except PackageNotFoundError:
        phi_version = "unknown"
    return (
        ("phi_version", _safe_text(phi_version, settings)),
        ("python_version", _safe_text(platform.python_version(), settings)),
        (
            "platform",
            _safe_text(f"{platform.system()} {platform.machine()}".strip(), settings),
        ),
        ("executable", _safe_text(sys.executable, settings)),
        ("cwd", _safe_text(str(cwd.expanduser().resolve(strict=False)), settings)),
    )


def _elapsed_ms(started: float) -> int:
    """把单调计时起点转换为非负毫秒整数。"""

    return max(0, round((perf_counter() - started) * 1000))


def _number(value: float) -> str:
    """以不带无意义小数点的形式展示有限整数浮点数。"""

    return str(int(value)) if math.isfinite(value) and value.is_integer() else str(value)
