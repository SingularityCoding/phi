from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from typing import TextIO

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from phi.cli.management import ContextCommandOutcome, DoctorCheck
from phi.mcp import ConfiguredMcpServer
from phi.sessions import SessionHandle


def _stream_is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, OSError):
        return False


def _terminal_width() -> int:
    columns = os.environ.get("COLUMNS", "").strip()
    if columns:
        try:
            return max(20, int(columns))
        except ValueError:
            pass
    return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _console(*, stderr: bool = False) -> Console:
    stream = sys.stderr if stderr else sys.stdout
    is_terminal = _stream_is_terminal(stream)
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


def render_empty_sessions() -> None:
    console = _console()
    console.print(Text("Sessions (0)", style="bold cyan"))
    console.print(Text("No Sessions found.", style="dim"))


def render_sessions(handles: Sequence[SessionHandle]) -> None:
    console = _console()
    table = Table(
        title=f"Sessions ({len(handles)})",
        title_style="bold cyan",
        header_style="bold cyan",
        box=box.ROUNDED,
        expand=True,
    )
    for label in ("ID", "Name", "Model", "Updated", "Origin", "Leaf"):
        table.add_column(label, overflow="fold")
    for handle in handles:
        metadata = handle.metadata
        table.add_row(
            Text(metadata.id),
            Text(metadata.name or "-"),
            Text(metadata.model or "-"),
            Text(metadata.updated_at.isoformat()),
            Text(metadata.origin),
            Text(metadata.leaf_id or "-"),
        )
    console.print(table)


def render_session_fork(session_id: str, parent_session_id: str, fork_point_entry_id: str) -> None:
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
    details.add_row(Text("New Session"), Text(session_id))
    details.add_row(Text("Parent Session"), Text(parent_session_id))
    details.add_row(Text("Fork point"), Text(fork_point_entry_id))
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
    console = _console()
    scope = "global" if global_only else "effective (project overrides global)"
    console.print(Text(f"Scope: {scope}", style="dim cyan"))
    if not servers:
        console.print(Text("MCP servers (0)", style="bold cyan"))
        console.print(Text("No MCP servers configured.", style="dim"))
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
    for server in servers:
        config = server.config
        state = "enabled" if config.enabled else "disabled"
        state_style = "green" if config.enabled else "dim"
        table.add_row(
            Text(server.server_id),
            Text(server.source),
            Text(state, style=state_style),
            Text(config.command),
            Text(shlex.join(config.args) if config.args else "-"),
            Text(", ".join(sorted(config.env)) if config.env else "-"),
        )
    console.print(table)


def render_doctor(checks: Sequence[DoctorCheck]) -> None:
    console = _console()
    table = Table(
        title="Doctor",
        title_style="bold cyan",
        header_style="bold cyan",
        box=box.ROUNDED,
        expand=True,
    )
    table.add_column("Status")
    table.add_column("Check", ratio=1, overflow="fold")
    status_styles = {"PASS": "bold green", "FAIL": "bold red", "SKIP": "bold yellow"}
    for check in checks:
        table.add_row(Text(check.status, style=status_styles[check.status]), Text(check.name))
    console.print(table)

    error_console = _console(stderr=True)
    for check in checks:
        if check.detail is not None:
            error_console.print(
                Text.assemble(
                    ("error: ", "bold red"),
                    Text(f"{check.name}: {check.detail}"),
                )
            )


def _known_value(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _json_syntax(value: object) -> Syntax:
    return Syntax(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        "json",
        theme="ansi_dark",
        background_color="default",
        word_wrap=True,
    )


def _message_label(message: dict[str, object]) -> tuple[str, str]:
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
    console = _console()
    inspection = outcome.inspection
    context = inspection.context
    metadata = outcome.handle.metadata

    overview = Table.grid()
    overview.add_column(overflow="fold")
    overview.add_row(Text.assemble(("Session ID: ", "bold cyan"), Text(outcome.handle.session_id)))
    overview.add_row(Text.assemble(("Session name: ", "bold cyan"), Text(metadata.name or "-")))
    overview.add_row(Text.assemble(("Model: ", "bold cyan"), Text(outcome.model_id)))
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

    console.print(
        Panel(
            Text(context.system_prompt),
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

    console.print(Text(f"Messages ({len(context.messages)})", style="bold cyan"))
    for index, message in enumerate(context.messages, start=1):
        label, style = _message_label(message)
        console.print(
            Panel(
                _json_syntax(message),
                title=Text(f"Message {index} · {label}", style=f"bold {style}"),
                border_style=style,
            )
        )

    console.print(
        Panel(
            Text(context.dropped_summary or "(none)"),
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
    _console(stderr=True).print(Text.assemble(("error: ", "bold red"), Text(message)))


def render_warning(message: str) -> None:
    _console(stderr=True).print(Text.assemble(("warning: ", "bold yellow"), Text(message)))


def render_confirmation(message: str) -> None:
    _console().print(Text(message, style="green"))


def render_failure(message: str) -> None:
    _console(stderr=True).print(Text(message, style="bold red"))


def render_exhausted(message: str) -> None:
    _console(stderr=True).print(Text(message, style="bold yellow"))


def render_cancelled(message: str) -> None:
    _console(stderr=True).print(Text(message, style="yellow"))
