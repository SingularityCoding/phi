"""为 Phi CLI 构建安全且适应终端宽度的 Rich 输出。"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TextIO

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from phi.cli.management import ContextCommandOutcome
from phi.doctor import DoctorCheck, DoctorReport, DoctorSection, DoctorStatus
from phi.mcp import ConfiguredMcpServer
from phi.sessions import SessionHandle, redact_text

_NARROW_RECORD_WIDTH = 100
_CREDENTIAL_ARGUMENT_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


def _stream_is_terminal(stream: TextIO) -> bool:
    """保守判断输出流是否连接到交互式终端。"""

    try:
        return stream.isatty()
    except (AttributeError, OSError):
        return False


def _terminal_width() -> int:
    """读取显式列宽或终端宽度，并保证最低可渲染宽度。"""

    columns = os.environ.get("COLUMNS", "").strip()
    if columns:
        try:
            return max(20, int(columns))
        except ValueError:
            pass
    return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _console(*, stderr: bool = False) -> Console:
    """按输出流能力构建禁用 markup 和自动高亮的 Rich Console。"""

    stream = sys.stderr if stderr else sys.stdout
    is_terminal = _stream_is_terminal(stream)
    # 颜色只在真实终端且用户未禁用时开启，管道输出保持稳定纯文本。
    color_enabled = (
        is_terminal
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
    )
    return Console(
        file=stream,
        width=_terminal_width() if is_terminal else max(160, _terminal_width()),
        force_terminal=color_enabled,
        color_system="standard" if color_enabled else None,
        no_color=not color_enabled,
        markup=False,
        highlight=False,
    )


def _escape_terminal_controls(value: str) -> str:
    """保留换行与 Tab，同时转义可操纵终端的其他控制字符。"""

    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character in {"\n", "\t"} or (codepoint >= 32 and not 127 <= codepoint <= 159):
            escaped.append(character)
        else:
            escaped.append(f"\\x{codepoint:02x}")
    return "".join(escaped)


def _literal_text(value: str, *, style: str | None = None) -> Text:
    """把外部文本包装为不会解释 Rich markup 的安全 Text。"""

    return Text(_escape_terminal_controls(value), style=style or "")


def _redact_mcp_text(value: str, secrets: Sequence[str]) -> str:
    """按已知环境值和凭证赋值模式脱敏一段 MCP 文本。"""

    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    redacted = redact_text(value, max_length=None)
    return _redact_credential_assignment(redacted) or redacted


def _is_credential_argument_name(value: str) -> bool:
    """识别常见凭证参数名及其带前缀变体。"""

    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return normalized in _CREDENTIAL_ARGUMENT_NAMES or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def _redact_credential_assignment(value: str) -> str | None:
    """若参数形如凭证赋值，则保留名称并隐藏其值。"""

    for separator in ("=", ":"):
        name, found, _ = value.partition(separator)
        if found and _is_credential_argument_name(name):
            return f"{name}{separator}[REDACTED]"
    return None


def _redact_mcp_arguments(arguments: Sequence[str], secrets: Sequence[str]) -> str:
    """脱敏 MCP 参数序列，并用 shell 可读形式重新组合。"""

    redacted: list[str] = []
    redact_next = False
    for argument in arguments:
        # ``--token VALUE`` 这类分离参数需要在识别名称后连带隐藏下一项。
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue

        safe_argument = _redact_mcp_text(argument, secrets)
        redacted.append(safe_argument)
        redact_next = _is_credential_argument_name(safe_argument)
    return shlex.join(redacted)


def _render_narrow_records(
    console: Console,
    *,
    title: str,
    noun: str,
    records: Sequence[Sequence[tuple[str, str, str | None]]],
) -> None:
    """在窄终端中把表格记录纵向展开为可折行字段。"""

    console.print(Text(title, style="bold cyan"))
    for index, fields in enumerate(records, start=1):
        if index > 1:
            console.print()
        console.print(Text(f"{noun} {index}", style="bold cyan"))
        for label, value, style in fields:
            console.print(
                Text.assemble(
                    (f"{label}: ", "bold cyan"),
                    _literal_text(value, style=style),
                ),
                overflow="fold",
            )


def render_empty_sessions() -> None:
    """显示确定的空 Session 列表状态。"""

    console = _console()
    console.print(Text("Sessions (0)", style="bold cyan"))
    console.print(Text("No Sessions found.", style="dim"))


def render_sessions(handles: Sequence[SessionHandle]) -> None:
    """按终端宽度把 Session handle 渲染为纵向记录或宽表格。"""

    console = _console()
    records = [
        (
            ("ID", handle.metadata.id, None),
            ("Name", handle.metadata.name or "-", None),
            ("Model", handle.metadata.model or "-", None),
            ("Updated", handle.metadata.updated_at.isoformat(), None),
            ("Origin", handle.metadata.origin, None),
            ("Leaf", handle.metadata.leaf_id or "-", None),
        )
        for handle in handles
    ]
    # 数据先统一成 records，再选择布局，保证宽窄模式展示同一组字段。
    if console.width < _NARROW_RECORD_WIDTH:
        _render_narrow_records(
            console,
            title=f"Sessions ({len(handles)})",
            noun="Session",
            records=records,
        )
        return

    table = Table(
        title=f"Sessions ({len(handles)})",
        title_style="bold cyan",
        header_style="bold cyan",
        box=box.ROUNDED,
        expand=True,
    )
    for label in ("ID", "Name", "Model", "Updated", "Origin", "Leaf"):
        table.add_column(label, overflow="fold")
    for fields in records:
        table.add_row(
            *(_literal_text(value, style=style) for _, value, style in fields),
        )
    console.print(table)


def render_session_fork(session_id: str, parent_session_id: str, fork_point_entry_id: str) -> None:
    """以脚本友好文本或交互式 Panel 显示 Fork 结果。"""

    # 非终端输出保持固定 key=value 契约，不让 Rich 布局影响脚本消费。
    if not _stream_is_terminal(sys.stdout):
        sys.stdout.write(
            f"session_id={session_id}\n"
            f"parent_session_id={parent_session_id}\n"
            f"fork_point_entry_id={fork_point_entry_id}\n"
        )
        sys.stdout.flush()
        return

    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold cyan")
    details.add_column(overflow="fold")
    details.add_row(Text("New Session"), _literal_text(session_id))
    details.add_row(Text("Parent Session"), _literal_text(parent_session_id))
    details.add_row(Text("Fork point"), _literal_text(fork_point_entry_id))
    _console().print(
        Panel(
            details,
            title=Text("Fork created", style="bold green"),
            border_style="green",
        )
    )


def render_mcp_servers(
    servers: Sequence[ConfiguredMcpServer],
    *,
    global_only: bool,
) -> None:
    """显示脱敏后的有效或用户级 MCP server 配置。"""

    console = _console()
    scope = "global" if global_only else "effective (project overrides global)"
    console.print(Text(f"Scope: {scope}", style="dim cyan"))
    if not servers:
        console.print(Text("MCP servers (0)", style="bold cyan"))
        console.print(Text("No MCP servers configured.", style="dim"))
        return

    # 只展示环境变量名称；值仅用于从命令和参数中清除可能泄漏的 secret。
    records: list[tuple[tuple[str, str, str | None], ...]] = []
    for server in servers:
        config = server.config
        state = "enabled" if config.enabled else "disabled"
        state_style = "green" if config.enabled else "dim"
        secrets = tuple(value for value in config.env.values() if value)

        records.append(
            (
                ("ID", server.server_id, None),
                ("Source", server.source, None),
                ("State", state, state_style),
                ("Command", _redact_mcp_text(config.command, secrets), None),
                (
                    "Arguments",
                    _redact_mcp_arguments(config.args, secrets) if config.args else "-",
                    None,
                ),
                ("Environment", ", ".join(sorted(config.env)) if config.env else "-", None),
            )
        )

    if console.width < _NARROW_RECORD_WIDTH:
        _render_narrow_records(
            console,
            title=f"MCP servers ({len(servers)})",
            noun="Server",
            records=records,
        )
        return

    table = Table(
        title=f"MCP servers ({len(servers)})",
        title_style="bold cyan",
        header_style="bold cyan",
        box=box.ROUNDED,
        expand=True,
    )
    for label in ("ID", "Source", "State", "Command", "Arguments", "Environment"):
        table.add_column(label, overflow="fold")
    for fields in records:
        table.add_row(
            *(_literal_text(value, style=style) for _, value, style in fields),
        )
    console.print(table)


_DOCTOR_SECTION_TITLES = {
    DoctorSection.CONFIGURATION: "Configuration",
    DoctorSection.WORKSPACE: "Workspace",
    DoctorSection.MODEL_GATEWAY: "Model gateway",
    DoctorSection.DEEP_CHECKS: "Deep checks",
}
_DOCTOR_STATUS_STYLES = {
    DoctorStatus.PASS: "bold green",
    DoctorStatus.WARN: "bold yellow",
    DoctorStatus.FAIL: "bold red",
    DoctorStatus.SKIP: "dim yellow",
}


@contextmanager
def doctor_progress(*, enabled: bool) -> Iterator[None]:
    """仅在交互终端中临时显示 doctor spinner。"""

    console = _console()
    if not enabled or not _stream_is_terminal(sys.stdout):
        yield
        return
    with console.status("Running Phi diagnostics…", spinner="dots"):
        yield


def render_doctor(report: DoctorReport, *, verbose: bool) -> None:
    """按分区渲染 doctor 报告、统计摘要和就绪结论。"""

    console = _console()
    console.print(Text("Phi doctor", style="bold cyan"))
    mode = (
        "Deep checks · starts enabled MCP servers · sends one streaming Model request"
        if report.mode == "deep"
        else "Standard checks · no Model response · no MCP server started"
    )
    console.print(Text(mode, style="dim"))

    # 环境事实只在 verbose 模式显示，默认输出聚焦可操作检查结果。
    if verbose:
        console.print()
        facts = Table.grid(padding=(0, 2))
        facts.add_column(style="bold cyan", no_wrap=True)
        facts.add_column(overflow="fold")
        for name, value in report.environment:
            facts.add_row(_doctor_fact_label(name), _literal_text(value))
        console.print(facts)

    # 枚举顺序是稳定的信息架构，不按动态状态重排分区。
    for section in DoctorSection:
        checks = tuple(check for check in report.checks if check.section is section)
        if not checks:
            continue
        console.print()
        console.print(Text(_DOCTOR_SECTION_TITLES[section], style="bold cyan"))
        if console.width < 80:
            _render_narrow_doctor_checks(console, checks, verbose=verbose)
        else:
            _render_wide_doctor_checks(console, checks, verbose=verbose)

    console.print()
    # FAIL 优先于 WARN 决定总结颜色；WARN 不会把健康报告变成失败。
    summary_style = "bold red" if not report.healthy else "bold green"
    if report.counts[DoctorStatus.WARN] and report.healthy:
        summary_style = "bold yellow"
    console.print(
        Text(
            f"{_doctor_summary(report)} · {report.duration_ms / 1000:.2f}s",
            style=summary_style,
        )
    )
    if not report.healthy:
        conclusion = "Phi is not ready to start a Run. Fix the failures and rerun doctor."
    elif report.counts[DoctorStatus.WARN]:
        conclusion = "Phi can start a Run. Optional configuration needs attention."
    else:
        conclusion = "Phi is ready to start a Run."
    console.print(_literal_text(conclusion))


def _render_wide_doctor_checks(
    console: Console,
    checks: Sequence[DoctorCheck],
    *,
    verbose: bool,
) -> None:
    """在宽终端中以三列网格渲染 doctor 检查。"""

    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(width=6, no_wrap=True)
    table.add_column(width=27, style="bold", overflow="fold")
    table.add_column(ratio=1, overflow="fold")
    for check in checks:
        summary = check.summary
        if verbose and check.duration_ms is not None:
            summary = f"{summary} · {check.duration_ms}ms"
        table.add_row(
            Text(check.status.value, style=_DOCTOR_STATUS_STYLES[check.status]),
            _literal_text(check.title),
            _literal_text(summary),
        )
        # 默认隐藏成功详情，失败/警告详情始终保留；verbose 展示全部。
        if check.status is not DoctorStatus.PASS or verbose:
            for detail in check.details:
                table.add_row("", "", _literal_text(detail, style=_doctor_detail_style(check)))
        if check.remediation is not None:
            table.add_row(
                "",
                "",
                Text.assemble(
                    ("Fix: ", _DOCTOR_STATUS_STYLES[check.status]),
                    _literal_text(check.remediation),
                ),
            )
    console.print(table)


def _render_narrow_doctor_checks(
    console: Console,
    checks: Sequence[DoctorCheck],
    *,
    verbose: bool,
) -> None:
    """在窄终端中逐项纵向渲染 doctor 检查。"""

    for index, check in enumerate(checks):
        if index:
            console.print()
        console.print(
            Text.assemble(
                (check.status.value, _DOCTOR_STATUS_STYLES[check.status]),
                "  ",
                _literal_text(check.title, style="bold"),
            )
        )
        summary = check.summary
        if verbose and check.duration_ms is not None:
            summary = f"{summary} · {check.duration_ms}ms"
        console.print(Text.assemble("  ", _literal_text(summary)), overflow="fold")
        if check.status is not DoctorStatus.PASS or verbose:
            for detail in check.details:
                console.print(
                    Text.assemble("  ", _literal_text(detail, style=_doctor_detail_style(check))),
                    overflow="fold",
                )
        if check.remediation is not None:
            console.print(
                Text.assemble(
                    ("  Fix: ", _DOCTOR_STATUS_STYLES[check.status]),
                    _literal_text(check.remediation),
                ),
                overflow="fold",
            )


def _doctor_detail_style(check: DoctorCheck) -> str:
    """根据检查状态选择详情文本的语义颜色。"""

    if check.status is DoctorStatus.FAIL:
        return "red"
    if check.status in {DoctorStatus.WARN, DoctorStatus.SKIP}:
        return "yellow"
    return "dim"


def _doctor_summary(report: DoctorReport) -> str:
    """按严重度顺序生成带正确单复数的检查计数。"""

    labels = {
        DoctorStatus.FAIL: ("failure", "failures"),
        DoctorStatus.WARN: ("warning", "warnings"),
        DoctorStatus.SKIP: ("skipped", "skipped"),
        DoctorStatus.PASS: ("passed", "passed"),
    }
    parts: list[str] = []
    for status in (DoctorStatus.FAIL, DoctorStatus.WARN, DoctorStatus.SKIP, DoctorStatus.PASS):
        count = report.counts[status]
        if count:
            singular, plural = labels[status]
            parts.append(f"{count} {singular if count == 1 else plural}")
    return " · ".join(parts)


def _doctor_fact_label(name: str) -> str:
    """把 doctor 环境字段映射为面向用户的标签。"""

    labels = {
        "phi_version": "Phi",
        "python_version": "Python",
        "platform": "Platform",
        "executable": "Executable",
        "cwd": "Workspace",
    }
    return labels.get(name, name.replace("_", " ").title())


def _known_value(value: int | None) -> str:
    """诚实显示已知整数，未知值不编造默认数。"""

    return "unknown" if value is None else str(value)


def _json_syntax(value: object) -> Syntax:
    """把对象编码成可折行 JSON Syntax，并转义 C1 控制字符。"""

    source = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    source = "".join(
        f"\\u{ord(character):04x}" if 127 <= ord(character) <= 159 else character
        for character in source
    )
    return Syntax(
        source,
        "json",
        theme="ansi_dark",
        background_color="default",
        word_wrap=True,
    )


def _message_label(message: dict[str, object]) -> tuple[str, str]:
    """按 Model 可见消息角色选择语义标签与边框颜色。"""

    role = message.get("role")
    if role == "tool":
        return "Tool Result", "yellow"
    if role == "assistant" and message.get("tool_calls"):
        return "Assistant · Tool Calls", "cyan"
    if role == "assistant":
        return "Assistant", "cyan"
    if role == "user":
        return "User", "green"
    return str(role or "Unknown").title(), "white"


def render_context(outcome: ContextCommandOutcome) -> None:
    """分块展示 Session、Context 来源和完整 Model 可见内容。"""

    console = _console()
    inspection = outcome.inspection
    context = inspection.context
    metadata = outcome.handle.metadata

    # Overview 只给出方向和容量来源；完整内容随后逐块展开，避免混淆层级。
    overview = Table.grid()
    overview.add_column(overflow="fold")
    overview.add_row(
        Text.assemble(("Session ID: ", "bold cyan"), _literal_text(outcome.handle.session_id))
    )
    overview.add_row(
        Text.assemble(("Session name: ", "bold cyan"), _literal_text(metadata.name or "-"))
    )
    overview.add_row(Text.assemble(("Model: ", "bold cyan"), _literal_text(outcome.model_id)))
    overview.add_row(
        Text.assemble(("Token Estimate: ", "bold cyan"), Text(str(inspection.estimate.tokens)))
    )
    overview.add_row(
        Text.assemble(
            ("Local Token Estimate: ", "bold cyan"),
            Text(str(inspection.estimate.local_tokens)),
        )
    )
    anchor = (
        "yes (provider Usage contributed)" if inspection.estimate.used_provider_anchor else "no"
    )
    overview.add_row(Text.assemble(("Provider Usage anchor: ", "bold cyan"), Text(anchor)))
    overview.add_row(
        Text.assemble(
            ("Effective input limit: ", "bold cyan"),
            Text(_known_value(inspection.effective_input_limit)),
        )
    )
    overview.add_row(
        Text.assemble(
            ("Safe input limit: ", "bold cyan"),
            Text(_known_value(inspection.safe_prompt_limit)),
        )
    )
    console.print(Panel(overview, title=Text("Overview", style="bold cyan"), border_style="cyan"))

    # 稳定指令与 Tool schemas 是每次请求的独立输入来源，分别呈现。
    console.print(
        Panel(
            _literal_text(context.system_prompt),
            title=Text("System prompt", style="bold cyan"),
            border_style="cyan",
        )
    )
    console.print(
        Panel(
            _json_syntax(list(context.tools)),
            title=Text(f"Tools ({len(context.tools)})", style="bold cyan"),
            border_style="cyan",
        )
    )

    # 每条消息保持精确 JSON 结构，Tool Call/Result 不退化为普通聊天文本。
    console.print(Text(f"Messages ({len(context.messages)})", style="bold cyan"))
    for index, message in enumerate(context.messages, start=1):
        label, style = _message_label(message)
        console.print(
            Panel(
                _json_syntax(message),
                title=_literal_text(f"Message {index} · {label}", style=f"bold {style}"),
                border_style=style,
            )
        )

    # Compaction 摘要和字符计数属于检查元数据，不伪装成 Conversation View 消息。
    console.print(
        Panel(
            _literal_text(context.dropped_summary or "(none)"),
            title=Text("Dropped-history summary", style="bold yellow"),
            border_style="yellow",
        )
    )
    console.print(
        Panel(
            _json_syntax(dict(inspection.character_counts)),
            title=Text("Character counts", style="bold cyan"),
            border_style="cyan",
        )
    )


def render_error(message: str) -> None:
    """把一般操作错误写入 stderr。"""

    _console(stderr=True).print(Text.assemble(("error: ", "bold red"), _literal_text(message)))


def render_warning(message: str) -> None:
    """把非致命诊断写入 stderr。"""

    _console(stderr=True).print(Text.assemble(("warning: ", "bold yellow"), _literal_text(message)))


def render_confirmation(message: str) -> None:
    """把成功确认写入 stdout。"""

    _console().print(_literal_text(message, style="green"))


def render_failure(message: str) -> None:
    """以失败样式写入 stderr。"""

    _console(stderr=True).print(_literal_text(message, style="bold red"))


def render_exhausted(message: str) -> None:
    """以 Step 预算耗尽样式写入 stderr。"""

    _console(stderr=True).print(_literal_text(message, style="bold yellow"))


def render_cancelled(message: str) -> None:
    """以取消样式写入 stderr。"""

    _console(stderr=True).print(_literal_text(message, style="yellow"))
