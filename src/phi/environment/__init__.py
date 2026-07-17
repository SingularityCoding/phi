"""Environment protocols and workspace-confined file operations."""

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
