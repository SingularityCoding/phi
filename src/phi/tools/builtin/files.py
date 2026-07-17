from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from phi.environment import FileError, FileSystem
from phi.tools.dispatcher import ToolFailure
from phi.tools.types import ApprovalClass, Injected, tool


def _file_failure(error: FileError) -> ToolFailure:
    return ToolFailure(f"file_{error.code.value}: {error.path}: {error.message}")


@tool(
    name="read",
    description="Read a bounded range of lines from a workspace text file.",
    approval_class=ApprovalClass.READ_ONLY,
)
async def read_file(
    path: str,
    filesystem: Injected[FileSystem],
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(gt=0)] = 200,
) -> str | ToolFailure:
    content = await filesystem.read_text(path)
    if isinstance(content, FileError):
        return _file_failure(content)
    return "".join(content.splitlines(keepends=True)[offset : offset + limit])


@tool(
    name="write",
    description="Create or replace a workspace text file.",
    approval_class=ApprovalClass.MUTATES_WORKSPACE,
)
async def write_file(
    path: str,
    content: str,
    filesystem: Injected[FileSystem],
) -> str | ToolFailure:
    result = await filesystem.write_text(path, content)
    if isinstance(result, FileError):
        return _file_failure(result)
    return f"wrote {len(content)} characters to {path}"


class EditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    old_text: Annotated[str, Field(min_length=1)]
    new_text: str


@tool(
    name="edit",
    description="Replace unique, non-overlapping fragments in a workspace text file.",
    approval_class=ApprovalClass.MUTATES_WORKSPACE,
)
async def edit_file(
    path: str,
    edits: Annotated[list[EditOperation], Field(min_length=1)],
    filesystem: Injected[FileSystem],
) -> str | ToolFailure:
    content = await filesystem.read_text(path)
    if isinstance(content, FileError):
        return _file_failure(content)

    replacements: list[tuple[int, int, str]] = []
    for edit in edits:
        matches: list[int] = []
        cursor = 0
        while (position := content.find(edit.old_text, cursor)) >= 0:
            matches.append(position)
            cursor = position + 1
        if not matches:
            return ToolFailure("edit_missing: an old_text fragment was not found")
        if len(matches) > 1:
            return ToolFailure("edit_non_unique: an old_text fragment matched more than once")
        start = matches[0]
        replacements.append((start, start + len(edit.old_text), edit.new_text))

    replacements.sort(key=lambda replacement: replacement[0])
    for previous, current in zip(replacements, replacements[1:], strict=False):
        if current[0] < previous[1]:
            return ToolFailure("edit_overlap: replacement ranges overlap")

    pieces: list[str] = []
    cursor = 0
    for start, end, new_text in replacements:
        pieces.extend((content[cursor:start], new_text))
        cursor = end
    pieces.append(content[cursor:])
    updated = "".join(pieces)

    result = await filesystem.write_text(path, updated)
    if isinstance(result, FileError):
        return _file_failure(result)
    return f"applied {len(edits)} edits to {path}"
