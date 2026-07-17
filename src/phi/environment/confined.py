from __future__ import annotations

import asyncio
import math
import os
import signal
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from phi.environment.protocol import (
    DirectoryEntry,
    ExecutionError,
    ExecutionErrorCode,
    FileError,
    FileErrorCode,
    ProcessResult,
)

DEFAULT_PROTECTED_PATTERNS = (
    ".git",
    ".git/**",
    "**/.git",
    "**/.git/**",
    ".env*",
    "**/.env*",
)


class ConfinedFileSystem:
    """Async file operations structurally confined to one canonical workspace root."""

    def __init__(self, root: Path, protected_patterns: Sequence[str]) -> None:
        self.root = root
        self.protected_patterns = tuple(protected_patterns)

    async def read_text(self, path: str) -> str | FileError:
        return await asyncio.to_thread(self._read_text, path)

    async def write_text(self, path: str, content: str) -> None | FileError:
        return await asyncio.to_thread(self._write_text, path, content)

    async def canonical_path(self, path: str) -> Path | FileError:
        return await asyncio.to_thread(self._resolve_existing, path)

    async def list_dir(self, path: str) -> tuple[DirectoryEntry, ...] | FileError:
        return await asyncio.to_thread(self._list_dir, path)

    def _candidate(self, path: str) -> Path | FileError:
        if not path or "\0" in path:
            return FileError(
                FileErrorCode.INVALID,
                path,
                "path must be non-empty and contain no NUL",
            )
        try:
            supplied = Path(path)
            candidate = supplied if supplied.is_absolute() else self.root / supplied
            lexical = Path(os.path.abspath(candidate))
            lexical.relative_to(self.root)
        except (OSError, ValueError):
            return FileError(FileErrorCode.PERMISSION_DENIED, path, "path is outside the workspace")
        return lexical

    def _resolve_existing(self, path: str) -> Path | FileError:
        candidate = self._candidate(path)
        if isinstance(candidate, FileError):
            return candidate
        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return _file_error(path, exc)
        denial = self._validate_canonical(canonical, path)
        return denial or canonical

    def _resolve_write_target(self, path: str) -> Path | FileError:
        candidate = self._candidate(path)
        if isinstance(candidate, FileError):
            return candidate
        try:
            if candidate.is_symlink() or candidate.exists():
                canonical = candidate.resolve(strict=True)
            else:
                canonical = candidate.parent.resolve(strict=True) / candidate.name
        except (OSError, RuntimeError) as exc:
            return _file_error(path, exc)
        denial = self._validate_canonical(canonical, path)
        return denial or canonical

    def _validate_canonical(self, canonical: Path, supplied_path: str) -> FileError | None:
        try:
            relative = canonical.relative_to(self.root)
        except ValueError:
            return FileError(
                FileErrorCode.PERMISSION_DENIED,
                supplied_path,
                "resolved path is outside the workspace",
            )
        relative_path = PurePosixPath(relative.as_posix())
        if relative_path != PurePosixPath(".") and any(
            relative_path.match(pattern) for pattern in self.protected_patterns
        ):
            return FileError(
                FileErrorCode.PERMISSION_DENIED,
                supplied_path,
                "path is protected",
            )
        return None

    def _read_text(self, path: str) -> str | FileError:
        canonical = self._resolve_existing(path)
        if isinstance(canonical, FileError):
            return canonical
        try:
            return canonical.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _file_error(path, exc)

    def _write_text(self, path: str, content: str) -> None | FileError:
        canonical = self._resolve_write_target(path)
        if isinstance(canonical, FileError):
            return canonical
        try:
            canonical.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _file_error(path, exc)
        return None

    def _list_dir(self, path: str) -> tuple[DirectoryEntry, ...] | FileError:
        canonical = self._resolve_existing(path)
        if isinstance(canonical, FileError):
            return canonical
        if not canonical.is_dir():
            return FileError(FileErrorCode.NOT_A_DIRECTORY, path, "path is not a directory")

        entries: list[DirectoryEntry] = []
        try:
            children = sorted(canonical.iterdir(), key=lambda child: child.name)
            for child in children:
                validated = self._resolve_existing(str(child))
                if isinstance(validated, FileError):
                    continue
                entries.append(
                    DirectoryEntry(
                        path=validated.relative_to(self.root).as_posix(),
                        name=child.name,
                        is_directory=validated.is_dir(),
                    )
                )
        except OSError as exc:
            return _file_error(path, exc)
        return tuple(entries)


class WorkspaceShell:
    """Unconfined local shell whose working directory is the workspace root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def exec(self, command: str, *, timeout_seconds: float) -> ProcessResult | ExecutionError:
        if not command.strip() or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            return ExecutionError(
                ExecutionErrorCode.INVALID,
                "command must be non-empty and timeout must be positive",
            )
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except ValueError as exc:
            return ExecutionError(ExecutionErrorCode.INVALID, str(exc))
        except OSError as exc:
            return ExecutionError(ExecutionErrorCode.LAUNCH_FAILED, str(exc))

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            _kill_process(process)
            await process.communicate()
            return ExecutionError(
                ExecutionErrorCode.TIMEOUT,
                f"command exceeded {timeout_seconds:g} seconds",
            )
        except asyncio.CancelledError:
            _kill_process(process)
            await process.communicate()
            raise

        return ProcessResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )


class ConfinedEnvironment:
    """Workspace-scoped FileSystem plus an accurately unconfined Shell."""

    def __init__(
        self,
        root: Path,
        *,
        protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
    ) -> None:
        try:
            canonical_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"workspace root cannot be resolved: {root}") from exc
        if not canonical_root.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        self.root = canonical_root
        self.filesystem = ConfinedFileSystem(canonical_root, protected_patterns)
        self.shell = WorkspaceShell(canonical_root)


def _file_error(path: str, exc: BaseException) -> FileError:
    if isinstance(exc, FileNotFoundError):
        code = FileErrorCode.NOT_FOUND
    elif isinstance(exc, PermissionError):
        code = FileErrorCode.PERMISSION_DENIED
    elif isinstance(exc, NotADirectoryError):
        code = FileErrorCode.NOT_A_DIRECTORY
    elif isinstance(exc, (IsADirectoryError, UnicodeError, ValueError, RuntimeError)):
        code = FileErrorCode.INVALID
    else:
        code = FileErrorCode.UNKNOWN
    return FileError(code, path, str(exc))


def _kill_process(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
