from __future__ import annotations

from pathlib import Path

from phi.environment import ConfinedEnvironment
from phi.model import ToolCall, ToolResult
from phi.tools import (
    BYPASS_MODE,
    RuleBasedApprovalPolicy,
    ToolDispatcher,
    build_default_registry,
)


def _dispatcher(root: Path) -> ToolDispatcher:
    environment = ConfinedEnvironment(root)
    return ToolDispatcher(
        build_default_registry(),
        RuleBasedApprovalPolicy(BYPASS_MODE),
        trusted_values={
            "filesystem": environment.filesystem,
            "shell": environment.shell,
        },
    )


async def test_ls_and_find_are_deterministic_and_confined(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a")
    (workspace / ".env").write_text("secret")
    nested = workspace / "zdir"
    nested.mkdir()
    (nested / "b.txt").write_text("b")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.txt").write_text("escaped")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    dispatcher = _dispatcher(workspace)

    listed = await dispatcher.dispatch(ToolCall(id="ls-1", name="ls", arguments={}))
    found = await dispatcher.dispatch(
        ToolCall(id="find-1", name="find", arguments={"pattern": "*.txt"})
    )

    assert listed == ToolResult(call_id="ls-1", output="a.txt\nzdir/")
    assert found == ToolResult(call_id="find-1", output="a.txt\nzdir/b.txt")


async def test_ls_and_find_accept_positive_result_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text(name)
    dispatcher = _dispatcher(workspace)

    listed = await dispatcher.dispatch(ToolCall(id="ls-limit", name="ls", arguments={"limit": 2}))
    found = await dispatcher.dispatch(
        ToolCall(
            id="find-limit",
            name="find",
            arguments={"pattern": "*.txt", "limit": 1},
        )
    )
    invalid = await dispatcher.dispatch(
        ToolCall(id="find-invalid", name="find", arguments={"pattern": "*", "limit": 0})
    )

    assert listed.output.splitlines() == ["a.txt", "b.txt"]
    assert found.output == "a.txt"
    assert invalid.error is not None and invalid.error.startswith("invalid_arguments:")


async def test_grep_counts_matches_not_context_lines_and_honors_glob_and_case(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("before\nNeedle\nafter\nneedle again\nend\n")
    (workspace / "ignored.txt").write_text("needle")
    (workspace / ".env").write_text("needle secret")

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="grep-1",
            name="grep",
            arguments={
                "pattern": "needle",
                "glob": "*.py",
                "case_sensitive": False,
                "context": 1,
                "limit": 1,
            },
        )
    )

    assert result == ToolResult(
        call_id="grep-1",
        output="a.py-1-before\na.py:2:Needle\na.py-3-after",
    )


async def test_grep_reports_invalid_patterns_and_positive_limit_validation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("text")
    dispatcher = _dispatcher(workspace)

    invalid_pattern = await dispatcher.dispatch(
        ToolCall(id="grep-pattern", name="grep", arguments={"pattern": "["})
    )
    invalid_limit = await dispatcher.dispatch(
        ToolCall(
            id="grep-limit",
            name="grep",
            arguments={"pattern": "text", "limit": 0},
        )
    )

    assert invalid_pattern.error is not None
    assert invalid_pattern.error.startswith("grep_invalid_pattern:")
    assert invalid_limit.error is not None
    assert invalid_limit.error.startswith("invalid_arguments:")


async def test_search_tools_apply_their_documented_default_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(1005):
        (workspace / f"item-{index:04}.txt").write_text("item")
    (workspace / "matches.log").write_text("".join(f"match {index}\n" for index in range(105)))
    dispatcher = _dispatcher(workspace)

    listed = await dispatcher.dispatch(ToolCall(id="ls-default", name="ls", arguments={}))
    found = await dispatcher.dispatch(
        ToolCall(id="find-default", name="find", arguments={"pattern": "*.txt"})
    )
    grepped = await dispatcher.dispatch(
        ToolCall(
            id="grep-default",
            name="grep",
            arguments={"pattern": "match", "path": "matches.log"},
        )
    )

    assert len(listed.output.splitlines()) == 500
    assert len(found.output.splitlines()) == 1000
    assert len(grepped.output.splitlines()) == 100
