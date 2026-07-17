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


async def test_read_returns_bounded_workspace_text(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("zero\none\ntwo\nthree\n")

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="read-1",
            name="read",
            arguments={"path": "notes.txt", "offset": 1, "limit": 2},
        )
    )

    assert result == ToolResult(call_id="read-1", output="one\ntwo\n")


async def test_read_denies_an_outside_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")

    result = await _dispatcher(workspace).dispatch(
        ToolCall(id="read-2", name="read", arguments={"path": str(outside)})
    )

    assert result.call_id == "read-2"
    assert result.output == ""
    assert result.error is not None
    assert result.error.startswith("file_permission_denied:")


async def test_write_creates_and_replaces_workspace_text(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dispatcher = _dispatcher(workspace)

    created = await dispatcher.dispatch(
        ToolCall(
            id="write-1",
            name="write",
            arguments={"path": "nested.txt", "content": "first"},
        )
    )
    replaced = await dispatcher.dispatch(
        ToolCall(
            id="write-2",
            name="write",
            arguments={"path": "nested.txt", "content": "second"},
        )
    )

    assert created.error is None
    assert replaced.error is None
    assert (workspace / "nested.txt").read_text() == "second"


async def test_file_tools_deny_traversal_symlink_escapes_and_protected_aliases(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    (workspace / ".env.local").write_text("TOKEN=secret")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("metadata")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    (workspace / "dotenv-link").symlink_to(workspace / ".env.local")
    dispatcher = _dispatcher(workspace)

    calls = (
        ToolCall(id="traversal", name="read", arguments={"path": "../outside/secret.txt"}),
        ToolCall(
            id="symlink-read",
            name="read",
            arguments={"path": "outside-link/secret.txt"},
        ),
        ToolCall(
            id="symlink-write",
            name="write",
            arguments={"path": "outside-link/new.txt", "content": "escaped"},
        ),
        ToolCall(id="protected", name="read", arguments={"path": ".env.local"}),
        ToolCall(id="protected-alias", name="read", arguments={"path": "dotenv-link"}),
        ToolCall(id="git-protected", name="read", arguments={"path": ".git/config"}),
    )

    results = [await dispatcher.dispatch(call) for call in calls]

    assert all(
        result.error and result.error.startswith("file_permission_denied:") for result in results
    )
    assert not (outside / "new.txt").exists()


async def test_write_validates_the_existing_parent_chain(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="missing-parent",
            name="write",
            arguments={"path": "missing/child.txt", "content": "no partial write"},
        )
    )

    assert result.error is not None
    assert result.error.startswith("file_not_found:")
    assert not (workspace / "missing").exists()


async def test_write_allows_creation_through_a_safe_resolved_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    safe_parent = workspace / "safe"
    safe_parent.mkdir()
    (workspace / "alias").symlink_to(safe_parent, target_is_directory=True)

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="safe-parent",
            name="write",
            arguments={"path": "alias/new.txt", "content": "safe"},
        )
    )

    assert result.error is None
    assert (safe_parent / "new.txt").read_text() == "safe"


async def test_file_error_categories_are_surfaced_by_builtins(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("text")
    (workspace / "directory").mkdir()
    dispatcher = _dispatcher(workspace)

    missing = await dispatcher.dispatch(
        ToolCall(id="missing", name="read", arguments={"path": "absent.txt"})
    )
    invalid = await dispatcher.dispatch(
        ToolCall(id="invalid", name="read", arguments={"path": "directory"})
    )
    not_directory = await dispatcher.dispatch(
        ToolCall(id="not-directory", name="ls", arguments={"path": "file.txt"})
    )

    assert missing.error is not None and missing.error.startswith("file_not_found:")
    assert invalid.error is not None and invalid.error.startswith("file_invalid:")
    assert not_directory.error is not None
    assert not_directory.error.startswith("file_not_a_directory:")


async def test_callers_can_replace_the_default_protected_patterns(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("allowed by explicit override")
    protected = workspace / "private"
    protected.mkdir()
    (protected / "secret.txt").write_text("denied")
    environment = ConfinedEnvironment(
        workspace,
        protected_patterns=("private", "private/**"),
    )
    dispatcher = ToolDispatcher(
        build_default_registry(),
        RuleBasedApprovalPolicy(BYPASS_MODE),
        trusted_values={
            "filesystem": environment.filesystem,
            "shell": environment.shell,
        },
    )

    dotenv = await dispatcher.dispatch(
        ToolCall(id="dotenv", name="read", arguments={"path": ".env"})
    )
    secret = await dispatcher.dispatch(
        ToolCall(id="secret", name="read", arguments={"path": "private/secret.txt"})
    )

    assert dotenv.output == "allowed by explicit override"
    assert secret.error is not None and secret.error.startswith("file_permission_denied:")


async def test_edit_applies_all_replacements_against_the_original_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "story.txt"
    target.write_text("one two")

    result = await _dispatcher(workspace).dispatch(
        ToolCall(
            id="edit-1",
            name="edit",
            arguments={
                "path": "story.txt",
                "edits": [
                    {"old_text": "one", "new_text": "two"},
                    {"old_text": "two", "new_text": "three"},
                ],
            },
        )
    )

    assert result.error is None
    assert target.read_text() == "two three"


async def test_edit_rejects_non_unique_or_overlapping_ranges_without_writing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "story.txt"
    original = "abc def abc"
    target.write_text(original)
    dispatcher = _dispatcher(workspace)

    non_unique = await dispatcher.dispatch(
        ToolCall(
            id="edit-duplicate",
            name="edit",
            arguments={
                "path": "story.txt",
                "edits": [{"old_text": "abc", "new_text": "x"}],
            },
        )
    )
    overlapping = await dispatcher.dispatch(
        ToolCall(
            id="edit-overlap",
            name="edit",
            arguments={
                "path": "story.txt",
                "edits": [
                    {"old_text": "abc d", "new_text": "left"},
                    {"old_text": "c def", "new_text": "right"},
                ],
            },
        )
    )

    assert non_unique.error is not None and non_unique.error.startswith("edit_non_unique:")
    assert overlapping.error is not None and overlapping.error.startswith("edit_overlap:")
    assert target.read_text() == original
