from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header


class PhiApp(App):
    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()


def run() -> None:
    PhiApp().run()
