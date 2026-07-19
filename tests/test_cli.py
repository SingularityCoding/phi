from typer.testing import CliRunner

from phi.cli import app

runner = CliRunner()


def test_bare_invocation_launches_tui(monkeypatch):
    # patch where it's looked up (phi.cli.main.run_tui), not where it's
    # defined (phi.ui.run) — `from x import y` binds a local name at import time.
    calls = []
    monkeypatch.setattr("phi.cli.main.run_tui", lambda: calls.append(True))

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert calls == [True]
