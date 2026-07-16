from __future__ import annotations

import typer

from phi.ui import run as run_tui

app = typer.Typer(help="phi — a small chat TUI.")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Launch the interactive TUI."""
    if ctx.invoked_subcommand is None:
        run_tui()
