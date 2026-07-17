from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import Field

from phi.environment import DirectoryEntry, FileError, FileErrorCode, FileSystem
from phi.tools.builtin.files import _file_failure
from phi.tools.dispatcher import ToolFailure
from phi.tools.types import ApprovalClass, Injected, tool

FIND_DEFAULT_LIMIT = 1000
GREP_DEFAULT_LIMIT = 100
LS_DEFAULT_LIMIT = 500


async def _walk(
    filesystem: FileSystem,
    path: str,
) -> tuple[list[DirectoryEntry], ToolFailure | None]:
    entries: list[DirectoryEntry] = []
    pending = [path]
    visited_directories: set[str] = set()
    while pending:
        directory = pending.pop()
        canonical = await filesystem.canonical_path(directory)
        if isinstance(canonical, FileError):
            return [], _file_failure(canonical)
        key = str(canonical)
        if key in visited_directories:
            continue
        visited_directories.add(key)

        listed = await filesystem.list_dir(directory)
        if isinstance(listed, FileError):
            return [], _file_failure(listed)
        entries.extend(listed)
        pending.extend(entry.path for entry in reversed(listed) if entry.is_directory)
    return entries, None


async def _files_under(
    filesystem: FileSystem,
    path: str,
) -> tuple[list[str], ToolFailure | None]:
    listed = await filesystem.list_dir(path)
    if isinstance(listed, FileError):
        if listed.code is FileErrorCode.NOT_A_DIRECTORY:
            canonical = await filesystem.canonical_path(path)
            if isinstance(canonical, FileError):
                return [], _file_failure(canonical)
            return [path], None
        return [], _file_failure(listed)
    entries, failure = await _walk(filesystem, path)
    if failure is not None:
        return [], failure
    return [entry.path for entry in entries if not entry.is_directory], None


@tool(
    name="find",
    description="Find workspace paths matching a glob through the confined FileSystem.",
    approval_class=ApprovalClass.READ_ONLY,
)
async def find_paths(
    pattern: str,
    filesystem: Injected[FileSystem],
    path: str = ".",
    limit: Annotated[int, Field(gt=0)] = FIND_DEFAULT_LIMIT,
) -> str | ToolFailure:
    entries, failure = await _walk(filesystem, path)
    if failure is not None:
        return failure
    matches = [entry.path for entry in entries if PurePosixPath(entry.path).match(pattern)][:limit]
    return "\n".join(matches)


@tool(
    name="ls",
    description="List a workspace directory through the confined FileSystem.",
    approval_class=ApprovalClass.READ_ONLY,
)
async def list_directory(
    filesystem: Injected[FileSystem],
    path: str = ".",
    limit: Annotated[int, Field(gt=0)] = LS_DEFAULT_LIMIT,
) -> str | ToolFailure:
    entries = await filesystem.list_dir(path)
    if isinstance(entries, FileError):
        return _file_failure(entries)
    rendered = [f"{entry.path}/" if entry.is_directory else entry.path for entry in entries[:limit]]
    return "\n".join(rendered)


@tool(
    name="grep",
    description="Search workspace text files with a regular expression.",
    approval_class=ApprovalClass.READ_ONLY,
)
async def grep_files(
    pattern: str,
    filesystem: Injected[FileSystem],
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = True,
    context: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(gt=0)] = GREP_DEFAULT_LIMIT,
) -> str | ToolFailure:
    try:
        expression = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        return ToolFailure(f"grep_invalid_pattern: {exc}")

    paths, failure = await _files_under(filesystem, path)
    if failure is not None:
        return failure

    rendered: list[str] = []
    matches_used = 0
    for file_path in paths:
        if glob is not None and not PurePosixPath(file_path).match(glob):
            continue
        content = await filesystem.read_text(file_path)
        if isinstance(content, FileError):
            return _file_failure(content)
        lines = content.splitlines()
        matching_lines = [index for index, line in enumerate(lines) if expression.search(line)]
        selected = matching_lines[: limit - matches_used]
        if not selected:
            continue
        matches_used += len(selected)
        selected_set = set(selected)
        rendered_indexes: set[int] = set()
        for index in selected:
            rendered_indexes.update(
                range(max(0, index - context), min(len(lines), index + context + 1))
            )
        for index in sorted(rendered_indexes):
            separator = ":" if index in selected_set else "-"
            rendered.append(f"{file_path}{separator}{index + 1}{separator}{lines[index]}")
        if matches_used == limit:
            break
    return "\n".join(rendered)
