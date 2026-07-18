from __future__ import annotations


class SessionError(Exception):
    """Base class for typed Session failures."""


class SessionNotFoundError(SessionError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session {session_id!r} was not found")


class IncompatibleSessionVersionError(SessionError):
    def __init__(self, session_id: str, version: object) -> None:
        self.session_id = session_id
        self.version = version
        super().__init__(f"Session {session_id!r} uses unsupported schema version {version!r}")


class CorruptSessionError(SessionError):
    def __init__(self, session_id: str, detail: str) -> None:
        self.session_id = session_id
        self.detail = detail
        super().__init__(f"Session {session_id!r} is corrupt: {detail}")


class StaleSessionHandleError(SessionError):
    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        self.session_id = session_id
        self.expected_revision = expected
        self.actual_revision = actual
        super().__init__(
            f"Session {session_id!r} handle is stale: expected revision {expected}, found {actual}"
        )


class InvalidSessionLeafError(SessionError):
    def __init__(self, session_id: str, leaf_id: str | None) -> None:
        self.session_id = session_id
        self.leaf_id = leaf_id
        super().__init__(f"Session {session_id!r} has invalid leaf {leaf_id!r}")


class MissingEntryParentError(SessionError):
    def __init__(self, session_id: str, entry_id: str, parent_id: str) -> None:
        self.session_id = session_id
        self.entry_id = entry_id
        self.parent_id = parent_id
        super().__init__(
            f"Entry {entry_id!r} in Session {session_id!r} has missing parent {parent_id!r}"
        )


class SessionLineageCycleError(SessionError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session lineage for {session_id!r} contains a cycle")
