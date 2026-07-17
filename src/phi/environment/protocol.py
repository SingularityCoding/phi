from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class FileErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    NOT_A_DIRECTORY = "not_a_directory"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileError:
    code: FileErrorCode
    path: str
    message: str


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    name: str
    is_directory: bool


class ExecutionErrorCode(StrEnum):
    TIMEOUT = "timeout"
    LAUNCH_FAILED = "launch_failed"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ExecutionError:
    code: ExecutionErrorCode
    message: str


class FileSystem(Protocol):
    async def read_text(self, path: str) -> str | FileError: ...

    async def write_text(self, path: str, content: str) -> None | FileError: ...

    async def canonical_path(self, path: str) -> Path | FileError: ...

    async def list_dir(self, path: str) -> tuple[DirectoryEntry, ...] | FileError: ...


class Shell(Protocol):
    async def exec(
        self, command: str, *, timeout_seconds: float
    ) -> ProcessResult | ExecutionError: ...
