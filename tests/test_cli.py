from pathlib import Path

import pytest
from typer.testing import CliRunner

from phi.cli import app

runner = CliRunner()


def test_bare_invocation_launches_tui(monkeypatch):
    # patch where it's looked up (phi.cli.main.run_tui), not where it's
    # defined (phi.ui.run) — `from x import y` binds a local name at import time.
    calls = []
    monkeypatch.setattr("phi.cli.main.run_tui", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert calls == [{"cwd": Path.cwd().resolve()}]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--help"], "session"),
        (["session", "--help"], "resume"),
        (["mcp", "--help"], "remove"),
        (["run", "--help"], "--max-steps"),
    ],
)
def test_root_group_and_leaf_help_remain_readable(
    arguments: list[str],
    expected: str,
) -> None:
    result = runner.invoke(app, arguments)

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert expected in result.stdout
    assert "Traceback" not in result.stdout
