"""通过受限 FileSystem 实现确定性的目录遍历、路径匹配和文本搜索。"""

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
    """深度优先遍历目录，并用规范路径避免符号链接形成环。"""

    entries: list[DirectoryEntry] = []
    pending = [path]
    visited_directories: set[str] = set()
    while pending:
        directory = pending.pop()
        # canonical_path 既执行 confinement 校验，也为去重提供稳定身份。
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
        # reversed 配合栈，使实际遍历仍遵循 list_dir 的正向稳定顺序。
        pending.extend(entry.path for entry in reversed(listed) if entry.is_directory)
    return entries, None


async def _files_under(
    filesystem: FileSystem,
    path: str,
) -> tuple[list[str], ToolFailure | None]:
    """把文件或目录输入统一展开为待搜索的文件路径列表。"""

    listed = await filesystem.list_dir(path)
    if isinstance(listed, FileError):
        if listed.code is FileErrorCode.NOT_A_DIRECTORY:
            # list_dir 的“非目录”同时承担区分单文件输入的作用。
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
    """在工作区目录树中查找匹配 glob 的路径。"""

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
    """列出目录的直接子项，并用尾斜杠标记子目录。"""

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
    """搜索文本文件，并按匹配条数而非渲染行数执行上限。"""

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
        # context 行不消耗 limit；只截取当前还可接受的真正匹配行。
        selected = matching_lines[: limit - matches_used]
        if not selected:
            continue
        matches_used += len(selected)
        selected_set = set(selected)
        rendered_indexes: set[int] = set()
        for index in selected:
            # 用集合合并相邻匹配的重叠上下文，避免同一行重复输出。
            rendered_indexes.update(
                range(max(0, index - context), min(len(lines), index + context + 1))
            )
        for index in sorted(rendered_indexes):
            # 冒号表示命中行，连字符表示仅作为上下文展示的行。
            separator = ":" if index in selected_set else "-"
            rendered.append(f"{file_path}{separator}{index + 1}{separator}{lines[index]}")
        if matches_used == limit:
            break
    return "\n".join(rendered)
