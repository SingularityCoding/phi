"""定义 Environment 向 Tool 层提供的异步文件与进程协议。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class FileErrorCode(StrEnum):
    """文件操作可预期失败的稳定分类。"""

    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    NOT_A_DIRECTORY = "not_a_directory"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileError:
    """把文件操作失败作为数据返回，避免由异常越过 Tool 边界。"""

    code: FileErrorCode
    path: str
    message: str


@dataclass(frozen=True)
class DirectoryEntry:
    """一次目录枚举得到的、相对工作区表示的条目。"""

    path: str
    name: str
    is_directory: bool


class ExecutionErrorCode(StrEnum):
    """Shell 启动或执行阶段可恢复错误的稳定分类。"""

    TIMEOUT = "timeout"
    LAUNCH_FAILED = "launch_failed"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProcessResult:
    """本地进程结束后的退出码及标准输出、标准错误。"""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ExecutionError:
    """把 Shell 的可预期失败表示为返回值。"""

    code: ExecutionErrorCode
    message: str


class FileSystem(Protocol):
    """Tool 可依赖的最小异步文件系统边界。"""

    async def read_text(self, path: str) -> str | FileError:
        """读取 UTF-8 文本，失败时返回结构化错误。"""
        ...

    async def write_text(self, path: str, content: str) -> None | FileError:
        """创建或覆盖 UTF-8 文本，成功时返回 ``None``。"""
        ...

    async def canonical_path(self, path: str) -> Path | FileError:
        """解析已存在路径，并返回经过边界检查的规范路径。"""
        ...

    async def list_dir(self, path: str) -> tuple[DirectoryEntry, ...] | FileError:
        """按稳定顺序列出目录中允许访问的直接子项。"""
        ...


class Shell(Protocol):
    """从工作区启动本地进程的异步边界；该协议不承诺路径受限。"""

    async def exec(self, command: str, *, timeout_seconds: float) -> ProcessResult | ExecutionError:
        """在给定超时内执行命令，并收集完整输出。"""
        ...
