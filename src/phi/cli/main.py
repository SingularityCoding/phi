from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from phi.bootstrap import build_host_runtime
from phi.cli.headless import execute_headless_run
from phi.harness import RunEvent, RunStatus
from phi.sessions import redact_text, serialize_run_event
from phi.ui import run as run_tui

app = typer.Typer(help="phi — an inspectable Agent Harness.")
_runtime_factory = build_host_runtime


class _JsonlEventWriter:
    async def emit(self, event: RunEvent) -> None:
        line = json.dumps(
            serialize_run_event(event),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stdout.write(line)
        sys.stdout.write("\n")
        sys.stdout.flush()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Launch the interactive TUI."""
    if ctx.invoked_subcommand is None:
        run_tui()


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
    events = _JsonlEventWriter() if json_output else None
    try:
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
                report_diagnostic=lambda value: typer.echo(
                    f"warning: {redact_text(value)}",
                    err=True,
                ),
            )
        )
    except KeyboardInterrupt:
        typer.echo("Run cancelled", err=True)
        raise typer.Exit(130) from None
    except Exception as error:
        message = redact_text(str(error)) or type(error).__name__
        typer.echo(f"error: {message}", err=True)
        raise typer.Exit(1) from None

    if outcome.result.status is RunStatus.COMPLETED:
        if not json_output:
            typer.echo(outcome.result.output)
        return
    if outcome.result.status is RunStatus.FAILED:
        assert outcome.result.error is not None
        typer.echo(f"Run failed: {redact_text(str(outcome.result.error))}", err=True)
        raise typer.Exit(1)
    if outcome.result.status is RunStatus.MAX_STEPS:
        typer.echo(f"Run exhausted its Step budget ({max_steps})", err=True)
        raise typer.Exit(2)
    typer.echo("Run cancelled", err=True)
    raise typer.Exit(130)
