"""实现受工作区路径约束的文件系统和明确不受限的本地 Shell。"""

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
    """把异步文件操作结构性地限制在一个规范化工作区根目录内。"""

    def __init__(self, root: Path, protected_patterns: Sequence[str]) -> None:
        """保存已规范化的根目录和根目录相对的保护模式。"""

        self.root = root
        self.protected_patterns = tuple(protected_patterns)

    async def read_text(self, path: str) -> str | FileError:
        """在线程中读取文本，避免同步文件 I/O 阻塞事件循环。"""

        return await asyncio.to_thread(self._read_text, path)

    async def write_text(self, path: str, content: str) -> None | FileError:
        """在线程中创建或覆盖工作区内的文本文件。"""

        return await asyncio.to_thread(self._write_text, path, content)

    async def canonical_path(self, path: str) -> Path | FileError:
        """返回已存在路径经过 confinement 检查后的规范形式。"""

        return await asyncio.to_thread(self._resolve_existing, path)

    async def list_dir(self, path: str) -> tuple[DirectoryEntry, ...] | FileError:
        """在线程中枚举目录，并隐藏不能安全解析的子项。"""

        return await asyncio.to_thread(self._list_dir, path)

    def _candidate(self, path: str) -> Path | FileError:
        """先做纯词法边界检查，不在此阶段跟随符号链接。"""

        # NUL 会让底层系统调用产生歧义；空路径也不应被默认为工作区根目录。
        if not path or "\0" in path:
            return FileError(
                FileErrorCode.INVALID,
                path,
                "path must be non-empty and contain no NUL",
            )
        try:
            supplied = Path(path)
            candidate = supplied if supplied.is_absolute() else self.root / supplied
            # abspath 消除 ``..``，但不会解析符号链接，因此这里只能阻止词法越界。
            lexical = Path(os.path.abspath(candidate))
            lexical.relative_to(self.root)
        except (OSError, ValueError):
            return FileError(FileErrorCode.PERMISSION_DENIED, path, "path is outside the workspace")
        return lexical

    def _resolve_existing(self, path: str) -> Path | FileError:
        """解析已存在路径的符号链接，再验证最终落点。"""

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
        """安全解析写入目标，兼容目标文件尚不存在的情况。"""

        candidate = self._candidate(path)
        if isinstance(candidate, FileError):
            return candidate
        try:
            if candidate.is_symlink() or candidate.exists():
                # 已存在目标必须解析自身，防止符号链接把写入导向工作区外。
                canonical = candidate.resolve(strict=True)
            else:
                # 新文件无法 strict resolve；改为验证已存在的父链，再拼回文件名。
                canonical = candidate.parent.resolve(strict=True) / candidate.name
        except (OSError, RuntimeError) as exc:
            return _file_error(path, exc)
        denial = self._validate_canonical(canonical, path)
        return denial or canonical

    def _validate_canonical(self, canonical: Path, supplied_path: str) -> FileError | None:
        """验证规范路径仍在根目录内，并拒绝受保护的相对路径。"""

        try:
            relative = canonical.relative_to(self.root)
        except ValueError:
            return FileError(
                FileErrorCode.PERMISSION_DENIED,
                supplied_path,
                "resolved path is outside the workspace",
            )
        relative_path = PurePosixPath(relative.as_posix())
        # 根目录本身可访问；其余路径以跨平台一致的 POSIX 形式匹配保护规则。
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
        """完成同步读取，并把预期的操作系统错误转换为数据。"""

        canonical = self._resolve_existing(path)
        if isinstance(canonical, FileError):
            return canonical
        try:
            return canonical.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _file_error(path, exc)

    def _write_text(self, path: str, content: str) -> None | FileError:
        """完成同步写入，并保持协议的结构化错误语义。"""

        canonical = self._resolve_write_target(path)
        if isinstance(canonical, FileError):
            return canonical
        try:
            canonical.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _file_error(path, exc)
        return None

    def _list_dir(self, path: str) -> tuple[DirectoryEntry, ...] | FileError:
        """枚举一个目录，只返回仍能通过 confinement 校验的直接子项。"""

        canonical = self._resolve_existing(path)
        if isinstance(canonical, FileError):
            return canonical
        if not canonical.is_dir():
            return FileError(FileErrorCode.NOT_A_DIRECTORY, path, "path is not a directory")

        entries: list[DirectoryEntry] = []
        try:
            # 先排序使 Tool 输出和测试结果不依赖文件系统的原生枚举顺序。
            children = sorted(canonical.iterdir(), key=lambda child: child.name)
            for child in children:
                validated = self._resolve_existing(str(child))
                if isinstance(validated, FileError):
                    # 一个不可访问或越界的子项不应泄露路径，也不应拖垮整个目录列表。
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
    """以工作区为 cwd 的本地 Shell；它明确不提供路径 confinement。"""

    def __init__(self, root: Path) -> None:
        """记录每个命令使用的固定工作目录。"""

        self.root = root

    async def exec(self, command: str, *, timeout_seconds: float) -> ProcessResult | ExecutionError:
        """执行 Shell 命令，超时或取消时终止整组子进程。"""

        if not command.strip() or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            return ExecutionError(
                ExecutionErrorCode.INVALID,
                "command must be non-empty and timeout must be positive",
            )
        try:
            # 新建进程会话让 POSIX 平台可以在取消时杀掉命令派生的整个进程组。
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
            # communicate 必须在 kill 后再次等待，否则子进程可能残留为僵尸进程。
            _kill_process(process)
            await process.communicate()
            return ExecutionError(
                ExecutionErrorCode.TIMEOUT,
                f"command exceeded {timeout_seconds:g} seconds",
            )
        except asyncio.CancelledError:
            # 取消是控制流而非 Tool Result；清理子进程后继续向上传播。
            _kill_process(process)
            await process.communicate()
            raise

        return ProcessResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )


class ConfinedEnvironment:
    """组合工作区受限的 FileSystem 与明确不受限的 Shell。"""

    def __init__(
        self,
        root: Path,
        *,
        protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
    ) -> None:
        """验证工作区根目录，并构造共享同一 cwd 的两个 Environment 边界。"""

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
    """把底层异常归一化为 FileSystem 协议公开的错误码。"""

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
    """尽力终止进程或 POSIX 进程组；进程已退出时视为清理成功。"""

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
