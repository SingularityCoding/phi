from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header

from phi.sessions import SessionHandle


class PhiApp(App):
    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        initial_session: SessionHandle | None = None,
        cwd: Path | None = None,
    ) -> None:
        super().__init__()
        self.current_session = initial_session
        self.cwd = cwd

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()


def run(
    *,
    initial_session: SessionHandle | None = None,
    cwd: Path | None = None,
) -> None:
    PhiApp(initial_session=initial_session, cwd=cwd).run()
