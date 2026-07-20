from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal, Never

import typer

from phi.bootstrap import build_headless_runtime, model_config_from_settings
from phi.cli.headless import execute_headless_run
from phi.cli.management import (
    execute_context_inspection,
    run_doctor,
    select_context_session,
)
from phi.cli.model_selection import require_available_model, require_explicit_model_id
from phi.cli.rendering import (
    render_cancelled,
    render_confirmation,
    render_context,
    render_doctor,
    render_empty_sessions,
    render_error,
    render_exhausted,
    render_failure,
    render_mcp_servers,
    render_session_fork,
    render_sessions,
    render_warning,
)
from phi.harness import RunEvent, RunStatus
from phi.mcp import add_mcp_server, list_configured_mcp_servers, remove_mcp_server
from phi.model import ModelConfig, ModelInfo, list_available_models
from phi.sessions import (
    SessionHandle,
    SessionStorage,
    fork_session,
    list_session_handles,
    redact_text,
    resume_session,
    serialize_run_event,
)
from phi.settings import Settings
from phi.ui import run as run_tui

app = typer.Typer(help="phi — an inspectable Agent Harness.")
session_app = typer.Typer(help="Inspect and branch durable Sessions.")
mcp_app = typer.Typer(help="Manage stdio MCP configuration sources.")
app.add_typer(session_app, name="session")
app.add_typer(mcp_app, name="mcp")
_runtime_factory = build_headless_runtime
_settings_factory: Callable[[], Settings] = Settings
_model_discovery: Callable[[ModelConfig], Coroutine[Any, Any, list[ModelInfo]]] = (
    list_available_models
)


def _global_mcp_path() -> Path:
    return Path("~/.phi/mcp.json").expanduser()


def _project_mcp_path(cwd: Path) -> Path:
    return cwd / ".phi" / "mcp.json"


class _JsonlEventWriter:
    def __init__(self, output: BinaryIO | None = None) -> None:
        self._output = sys.stdout.buffer if output is None else output
        self._failure: Exception | None = None

    async def emit(self, event: RunEvent) -> None:
        if self._failure is not None:
            raise self._failure
        try:
            line = json.dumps(
                serialize_run_event(event),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload = f"{line}\n".encode()
            written = self._output.write(payload)
            if written is not None and written != len(payload):
                raise OSError(f"wrote {written} of {len(payload)} JSONL bytes")
            self._output.flush()
        except Exception as error:
            self._failure = error
            raise

    def raise_if_failed(self) -> None:
        """Restore fail-closed Host semantics after best-effort Event delivery."""

        if self._failure is not None:
            raise OSError(f"failed to write JSONL Events: {self._failure}") from self._failure


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Launch the interactive TUI."""
    if ctx.invoked_subcommand is None:
        run_tui(cwd=Path.cwd().resolve())


def _run_async[T](operation: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(operation)
    except KeyboardInterrupt:
        render_cancelled("Operation cancelled")
        raise typer.Exit(130) from None
    except Exception as error:
        _exit_operational(error)


def _exit_operational(error: Exception) -> Never:
    """Render one redacted operational failure without exposing a traceback."""

    message = redact_text(str(error)) or type(error).__name__
    render_error(message)
    raise typer.Exit(1) from None


def _session_storage() -> SessionStorage:
    return SessionStorage(_load_settings().session_dir)


def _load_settings() -> Settings:
    try:
        return _settings_factory()
    except Exception as error:
        _exit_operational(error)


async def _discover_models(settings: Settings) -> list[ModelInfo]:
    return await _model_discovery(model_config_from_settings(settings))


def _report_session_diagnostics(handles: list[SessionHandle]) -> None:
    reported: set[str] = set()
    for handle in handles:
        for diagnostic in handle.diagnostics:
            if diagnostic not in reported:
                reported.add(diagnostic)
                render_warning(redact_text(diagnostic))


def _report_diagnostics(diagnostics: tuple[str, ...]) -> None:
    for diagnostic in diagnostics:
        render_warning(redact_text(diagnostic))


@session_app.command("list")
def session_list_command() -> None:
    """List all validated Sessions, newest first."""

    handles = _run_async(list_session_handles(_session_storage()))
    handles.sort(
        key=lambda handle: (handle.metadata.updated_at, handle.session_id),
        reverse=True,
    )
    _report_session_diagnostics(handles)
    if not handles:
        render_empty_sessions()
        return
    render_sessions(handles)


@session_app.command("resume")
def session_resume_command(
    session_id: Annotated[str, typer.Argument(metavar="ID")],
) -> None:
    """Resume one validated Session in the Textual Host."""

    handle = _run_async(resume_session(_session_storage(), session_id))
    _report_session_diagnostics([handle])
    run_tui(initial_session=handle, cwd=Path.cwd())


@session_app.command("fork")
def session_fork_command(
    session_id: Annotated[str, typer.Argument(metavar="ID")],
    entry_id: Annotated[str, typer.Argument(metavar="ENTRY_ID")],
    model: Annotated[str | None, typer.Option("--model")] = None,
) -> None:
    """Create an exact Fork at an Entry in the selected Conversation View."""

    settings = _load_settings()
    storage = SessionStorage(settings.session_dir)
    source = _run_async(resume_session(storage, session_id))
    _report_session_diagnostics([source])
    selected_model: str | None = None
    if model is not None:
        try:
            selected_model = require_explicit_model_id(model)
        except Exception as error:
            _exit_operational(error)
        models = _run_async(_discover_models(settings))
        try:
            require_available_model(selected_model, models)
        except Exception as error:
            _exit_operational(error)
    forked = _run_async(
        fork_session(
            storage,
            source,
            entry_id,
            model=selected_model,
        )
    )
    render_session_fork(forked.session_id, source.session_id, entry_id)


@app.command("context")
def context_command(
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect the complete Context Phi would send to the Model."""

    settings = _load_settings()
    selected = _run_async(select_context_session(SessionStorage(settings.session_dir), session_id))
    outcome = _run_async(
        execute_context_inspection(
            cwd=Path.cwd(),
            runtime_factory=_runtime_factory,
            selected_session=selected,
        )
    )
    _report_diagnostics(outcome.diagnostics)
    if json_output:
        typer.echo(
            json.dumps(
                outcome.to_document(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    render_context(outcome)


def _selected_mcp_source(
    global_scope: bool,
) -> tuple[Literal["project", "global"], Path]:
    if global_scope:
        return "global", _global_mcp_path()
    return "project", _project_mcp_path(Path.cwd())


@mcp_app.command("add")
def mcp_add_command(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    command: Annotated[list[str], typer.Argument(metavar="COMMAND [ARGS...]")],
    global_scope: Annotated[bool, typer.Option("--global")] = False,
) -> None:
    """Add one stdio MCP server; tokens after -- are literal command data."""

    source, path = _selected_mcp_source(global_scope)
    _run_async(
        add_mcp_server(
            path,
            name,
            command[0],
            tuple(command[1:]),
            source=source,
        )
    )
    render_confirmation(f"Added {source} MCP server {name!r}.")


@mcp_app.command("list")
def mcp_list_command(
    global_scope: Annotated[bool, typer.Option("--global")] = False,
) -> None:
    """List effective merged or global-only MCP configuration."""

    servers = _run_async(
        list_configured_mcp_servers(
            _global_mcp_path(),
            _project_mcp_path(Path.cwd()),
            global_only=global_scope,
        )
    )
    render_mcp_servers(servers, global_only=global_scope)


@mcp_app.command("remove")
def mcp_remove_command(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    global_scope: Annotated[bool, typer.Option("--global")] = False,
) -> None:
    """Remove one MCP server from exactly one selected source."""

    source, path = _selected_mcp_source(global_scope)
    _run_async(remove_mcp_server(path, name, source=source))
    render_confirmation(f"Removed {source} MCP server {name!r}.")


@app.command("doctor")
def doctor_command() -> None:
    """Validate Model settings, Proxy discovery, and the configured default Model."""

    report = _run_async(
        run_doctor(
            settings_factory=_settings_factory,
            model_discovery=_model_discovery,
        )
    )
    render_doctor(report.checks)
    if not report.healthy:
        raise typer.Exit(1)


@app.command("run")
def run_command(
    task: Annotated[str, typer.Argument(help="One task for the Agent to handle.")],
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    max_steps: Annotated[int, typer.Option("--max-steps", min=1)] = 20,
    model: Annotated[str | None, typer.Option("--model")] = None,
) -> None:
    """Run one persistent headless Agent task."""

    if not task.strip():
        raise typer.BadParameter("TASK must contain non-whitespace text", param_hint="TASK")
    events: _JsonlEventWriter | None = None
    try:
        if json_output:
            events = _JsonlEventWriter()
        outcome = asyncio.run(
            execute_headless_run(
                task,
                cwd=Path.cwd(),
                runtime_factory=_runtime_factory,
                session_id=session_id,
                selected_model=model,
                max_steps=max_steps,
                events=events,
                report_session=lambda value: typer.echo(f"session_id={value}", err=True),
                report_diagnostic=lambda value: render_warning(redact_text(value)),
            )
        )
        if events is not None:
            events.raise_if_failed()
    except KeyboardInterrupt:
        render_cancelled("Run cancelled")
        raise typer.Exit(130) from None
    except Exception as error:
        _exit_operational(error)

    if outcome.result.status is RunStatus.COMPLETED:
        if not json_output:
            typer.echo(outcome.result.output)
        return
    if outcome.result.status is RunStatus.FAILED:
        assert outcome.result.error is not None
        render_failure(f"Run failed: {redact_text(str(outcome.result.error))}")
        raise typer.Exit(1)
    if outcome.result.status is RunStatus.MAX_STEPS:
        render_exhausted(f"Run exhausted its Step budget ({max_steps})")
        raise typer.Exit(2)
    render_cancelled("Run cancelled")
    raise typer.Exit(130)
