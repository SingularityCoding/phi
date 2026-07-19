from __future__ import annotations

import os
from pathlib import Path

import pytest

from .support import (
    WorkspaceSnapshotError,
    compare_workspace_snapshots,
    snapshot_workspace,
)


def test_workspace_snapshot_reports_exact_regular_file_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "unchanged.bin").write_bytes(b"\x00\xffstable")
    (workspace / "modified.txt").write_text("before\n", encoding="utf-8")
    (workspace / "deleted.txt").write_text("remove\n", encoding="utf-8")
    before = snapshot_workspace(workspace)

    (workspace / "modified.txt").write_text("after\n", encoding="utf-8")
    (workspace / "deleted.txt").unlink()
    (workspace / "nested").mkdir()
    (workspace / "nested" / "created.json").write_bytes(b'{"ok":true}\n')
    after = snapshot_workspace(workspace)

    delta = compare_workspace_snapshots(before, after)

    assert before.read("unchanged.bin") == b"\x00\xffstable"
    assert after.read("unchanged.bin") == b"\x00\xffstable"
    assert delta.created == ("nested/created.json",)
    assert delta.modified == ("modified.txt",)
    assert delta.deleted == ("deleted.txt",)


def test_workspace_snapshot_rejects_a_symlink_instead_of_following_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)

    with pytest.raises(WorkspaceSnapshotError, match="symlink.*escape"):
        snapshot_workspace(workspace)


def test_workspace_snapshot_rejects_an_unreadable_regular_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unreadable = workspace / "unreadable.txt"
    unreadable.write_text("hidden\n", encoding="utf-8")
    unreadable.chmod(0)
    try:
        with pytest.raises(WorkspaceSnapshotError, match="unreadable.*unreadable.txt"):
            snapshot_workspace(workspace)
    finally:
        unreadable.chmod(0o600)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFO support")
def test_workspace_snapshot_rejects_non_regular_objects(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "events.fifo")

    with pytest.raises(WorkspaceSnapshotError, match="non-regular.*events.fifo"):
        snapshot_workspace(workspace)
