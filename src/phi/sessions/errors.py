"""Session 存储、树遍历和并发提交产生的类型化错误。"""

from __future__ import annotations


class SessionError(Exception):
    """所有类型化 Session 失败的基类。"""


class SessionNotFoundError(SessionError):
    """请求的 Session 不存在。"""

    def __init__(self, session_id: str) -> None:
        """记录缺失的 Session ID，并构造稳定错误消息。"""

        self.session_id = session_id
        super().__init__(f"Session {session_id!r} was not found")


class IncompatibleSessionVersionError(SessionError):
    """持久化文档的 schema 版本不受当前实现支持。"""

    def __init__(self, session_id: str, version: object) -> None:
        """记录 Session ID 与无法识别的 schema 版本。"""

        self.session_id = session_id
        self.version = version
        super().__init__(f"Session {session_id!r} uses unsupported schema version {version!r}")


class CorruptSessionError(SessionError):
    """Session 文件存在无法安全恢复的结构或提交错误。"""

    def __init__(self, session_id: str, detail: str) -> None:
        """记录损坏的 Session ID 与面向调用方的失败细节。"""

        self.session_id = session_id
        self.detail = detail
        super().__init__(f"Session {session_id!r} is corrupt: {detail}")


class StaleSessionHandleError(SessionError):
    """调用方用旧 revision 的 SessionHandle 尝试写入。"""

    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        """记录调用方预期 revision 与磁盘上的实际 revision。"""

        self.session_id = session_id
        self.expected_revision = expected
        self.actual_revision = actual
        super().__init__(
            f"Session {session_id!r} handle is stale: expected revision {expected}, found {actual}"
        )


class InvalidSessionLeafError(SessionError):
    """选定 leaf 不是当前 Session 可到达的 Entry。"""

    def __init__(self, session_id: str, leaf_id: str | None) -> None:
        """记录 Session ID 与无效 leaf ID。"""

        self.session_id = session_id
        self.leaf_id = leaf_id
        super().__init__(f"Session {session_id!r} has invalid leaf {leaf_id!r}")


class MissingEntryParentError(SessionError):
    """Entry 引用的父节点不在可解析的 Session 谱系中。"""

    def __init__(self, session_id: str, entry_id: str, parent_id: str) -> None:
        """记录断裂引用所在 Session、Entry 与缺失父节点。"""

        self.session_id = session_id
        self.entry_id = entry_id
        self.parent_id = parent_id
        super().__init__(
            f"Entry {entry_id!r} in Session {session_id!r} has missing parent {parent_id!r}"
        )


class SessionLineageCycleError(SessionError):
    """Session 的父谱系出现环，无法继续安全遍历。"""

    def __init__(self, session_id: str) -> None:
        """记录检测到谱系环的 Session ID。"""

        self.session_id = session_id
        super().__init__(f"Session lineage for {session_id!r} contains a cycle")
