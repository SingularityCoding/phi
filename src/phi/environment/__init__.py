"""导出 Environment 协议、结果类型与工作区受限的本地实现。"""

from phi.environment.confined import (
    DEFAULT_PROTECTED_PATTERNS,
    ConfinedEnvironment,
)
from phi.environment.protocol import (
    DirectoryEntry,
    ExecutionError,
    ExecutionErrorCode,
    FileError,
    FileErrorCode,
    FileSystem,
    ProcessResult,
    Shell,
)

__all__ = [
    "DEFAULT_PROTECTED_PATTERNS",
    "ConfinedEnvironment",
    "DirectoryEntry",
    "ExecutionError",
    "ExecutionErrorCode",
    "FileError",
    "FileErrorCode",
    "FileSystem",
    "ProcessResult",
    "Shell",
]
