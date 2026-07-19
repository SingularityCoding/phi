from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class WorkspaceSnapshotError(RuntimeError):
    """The workspace could not be represented with conservative file semantics."""


def validate_workspace_relative_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if not path or path == "." or parsed.is_absolute() or ".." in parsed.parts or "\\" in path:
        raise ValueError(f"path must be workspace-relative: {path!r}")


def format_paths(paths: tuple[str, ...] | list[str]) -> str:
    return f"[{', '.join(paths)}]"


@dataclass(frozen=True, order=True)
class WorkspaceFile:
    path: str
    content: bytes = field(repr=False, compare=True)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """An immutable, byte-preserving view of regular workspace files."""

    files: tuple[WorkspaceFile, ...]

    def read(self, path: str) -> bytes | None:
        return next((item.content for item in self.files if item.path == path), None)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)


@dataclass(frozen=True)
class WorkspaceDelta:
    created: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]


def snapshot_workspace(root: Path) -> WorkspaceSnapshot:
    """Read regular files without following workspace symlinks."""

    expanded = root.expanduser()
    if expanded.is_symlink():
        raise WorkspaceSnapshotError(f"workspace root is a symlink: {expanded}")
    try:
        canonical_root = expanded.resolve(strict=True)
    except OSError as error:
        raise WorkspaceSnapshotError(f"workspace root is unavailable: {expanded}") from error
    if not canonical_root.is_dir():
        raise WorkspaceSnapshotError(f"workspace root is not a directory: {canonical_root}")

    files: list[WorkspaceFile] = []
    _snapshot_directory(canonical_root, canonical_root, files)
    return WorkspaceSnapshot(tuple(sorted(files)))


def compare_workspace_snapshots(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> WorkspaceDelta:
    before_files = {item.path: item.content for item in before.files}
    after_files = {item.path: item.content for item in after.files}
    before_paths = set(before_files)
    after_paths = set(after_files)
    return WorkspaceDelta(
        created=tuple(sorted(after_paths - before_paths)),
        modified=tuple(
            sorted(
                path
                for path in before_paths & after_paths
                if before_files[path] != after_files[path]
            )
        ),
        deleted=tuple(sorted(before_paths - after_paths)),
    )


def _snapshot_directory(root: Path, directory: Path, files: list[WorkspaceFile]) -> None:
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as error:
        relative = directory.relative_to(root).as_posix() or "."
        raise WorkspaceSnapshotError(f"unreadable directory: {relative}") from error

    for entry in entries:
        path = Path(entry.path)
        relative = path.relative_to(root).as_posix()
        try:
            before = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise WorkspaceSnapshotError(f"unreadable workspace object: {relative}") from error
        mode = before.st_mode
        if stat.S_ISLNK(mode):
            raise WorkspaceSnapshotError(f"symlink workspace object: {relative}")
        if stat.S_ISDIR(mode):
            _snapshot_directory(root, path, files)
            continue
        if not stat.S_ISREG(mode):
            raise WorkspaceSnapshotError(f"non-regular workspace object: {relative}")
        if mode & 0o444 == 0:
            raise WorkspaceSnapshotError(f"unreadable regular file: {relative}")
        content = _read_stable_regular_file(path, relative, before)
        files.append(WorkspaceFile(relative, content))


def _read_stable_regular_file(path: Path, relative: str, before: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkspaceSnapshotError(f"unreadable regular file: {relative}") from error
    try:
        opened = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(opened):
            raise WorkspaceSnapshotError(f"workspace file changed while snapshotting: {relative}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read()
            after_read = os.fstat(stream.fileno())
    except OSError as error:
        raise WorkspaceSnapshotError(f"unreadable regular file: {relative}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        after_path = path.stat(follow_symlinks=False)
    except OSError as error:
        raise WorkspaceSnapshotError(
            f"workspace file changed while snapshotting: {relative}"
        ) from error
    if _file_identity(opened) != _file_identity(after_read) or _file_identity(
        after_read
    ) != _file_identity(after_path):
        raise WorkspaceSnapshotError(f"workspace file changed while snapshotting: {relative}")
    return content


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
