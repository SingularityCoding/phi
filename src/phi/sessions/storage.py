"""以 JSONL journal 和原子元数据提交实现持久化 Session 存储。"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from phi.sessions.entries import Entry, dump_entry, parse_entry
from phi.sessions.errors import (
    CorruptSessionError,
    IncompatibleSessionVersionError,
    InvalidSessionLeafError,
    MissingEntryParentError,
    SessionLineageCycleError,
    SessionNotFoundError,
    StaleSessionHandleError,
)
from phi.sessions.metadata import SCHEMA_VERSION, SessionMetadata, SessionMetadataEnvelope

# 同一进程中，共享根目录与 Session ID 的多个 Storage 实例必须共用写锁。
_SESSION_LOCKS: dict[tuple[Path, str], asyncio.Lock] = {}


@dataclass(frozen=True)
class LoadedSession:
    """一次一致性读取所得的提交信封、Entries 与恢复诊断。"""

    envelope: SessionMetadataEnvelope
    entries: tuple[Entry, ...]
    diagnostics: tuple[str, ...] = ()


class SessionStorage:
    """默认位于工作区外、带 schema 版本的磁盘 Session 存储。"""

    def __init__(self, root: Path | str = Path("~/.phi/sessions")) -> None:
        """初始化存储根目录；实际目录在首次写入时创建。"""

        self.root = Path(root).expanduser()

    def journal_path(self, session_id: str) -> Path:
        """返回保存 Conversation Entries 的 JSONL journal 路径。"""

        return self._session_path(session_id, ".jsonl")

    def metadata_path(self, session_id: str) -> Path:
        """返回 Session 原子提交元数据的路径。"""

        return self._session_path(session_id, ".metadata.json")

    def trace_path(self, session_id: str) -> Path:
        """返回独立 Trace 记录的路径；Trace 不参与恢复 Conversation View。"""

        return self._session_path(session_id, ".trace.jsonl")

    def _session_path(self, session_id: str, suffix: str) -> Path:
        """校验文件名片段并构造 Session 产品路径。"""

        # Session ID 不允许携带路径语义，防止越过配置的存储根目录。
        if (
            not session_id.strip()
            or session_id in {".", ".."}
            or any(character in session_id for character in ("/", "\\", "\0"))
        ):
            raise SessionNotFoundError(session_id)
        return self.root / f"{session_id}{suffix}"

    async def create(
        self,
        *,
        model: str | None = None,
        name: str | None = None,
        parent_session_id: str | None = None,
        fork_point_entry_id: str | None = None,
        origin: str = "new",
    ) -> SessionMetadataEnvelope:
        """创建一个空 Session，并持久化 revision 0 的提交状态。

        journal 与 metadata 必须成对出现；协程取消也会等待后台创建结果并在必要时
        回滚，避免留下半创建 Session。
        """

        session_id = str(uuid4())
        now = datetime.now(UTC)
        metadata = SessionMetadata.model_validate(
            {
                "id": session_id,
                "created_at": now,
                "updated_at": now,
                "leaf_id": fork_point_entry_id if origin == "fork" else None,
                "parent_session_id": parent_session_id,
                "fork_point_entry_id": fork_point_entry_id,
                "name": name,
                "model": model,
                "origin": origin,
            }
        )
        envelope = SessionMetadataEnvelope(
            revision=0,
            committed_entry_count=0,
            metadata=metadata,
        )
        # 屏蔽外层取消，确保线程中的文件事务先落定，再决定返回或清理。
        creation = asyncio.ensure_future(asyncio.to_thread(self._create_sync, envelope))
        try:
            await asyncio.shield(creation)
        except BaseException:
            creation_succeeded = False
            try:
                await asyncio.shield(creation)
            except BaseException:
                pass
            else:
                creation_succeeded = True
            if creation_succeeded:
                await asyncio.to_thread(self._remove_session_files_sync, session_id)
            raise
        return envelope

    async def load(self, session_id: str) -> SessionMetadataEnvelope:
        """只读取并校验一个 Session 的提交元数据。"""

        return await asyncio.to_thread(self._load_sync, session_id)

    async def load_state(self, session_id: str) -> LoadedSession:
        """读取一个 Session 的已提交 Entries 与元数据一致快照。"""

        return await asyncio.to_thread(self._load_state_sync, session_id)

    async def list_metadata(self) -> list[SessionMetadataEnvelope]:
        """按创建时间与 ID 的稳定顺序列出全部 Session 元数据。"""

        return await asyncio.to_thread(self._list_metadata_sync)

    async def rollback_empty_subagent(self, session_id: str) -> None:
        """Delegation 启动事务失败后，删除刚创建且仍为空的 Subagent Session。"""

        async with self._lock(session_id):
            state = await self.load_state(session_id)
            if (
                state.envelope.metadata.origin != "subagent"
                or state.envelope.revision != 0
                or state.entries
            ):
                raise CorruptSessionError(
                    session_id,
                    "only a new empty Subagent Session can be rolled back",
                )
            await asyncio.to_thread(self._rollback_empty_subagent_sync, session_id)

    async def replace_metadata(
        self,
        session_id: str,
        *,
        expected_revision: int,
        metadata: SessionMetadata,
    ) -> SessionMetadataEnvelope:
        """在预期 revision 上原子替换元数据，不改动 Entry journal。"""

        async with self._lock(session_id):
            current_state = await self.load_state(session_id)
            current = current_state.envelope
            if current.revision != expected_revision:
                # 乐观并发控制禁止旧 SessionHandle 覆盖较新的分支选择或名称。
                raise StaleSessionHandleError(
                    session_id,
                    expected_revision,
                    current.revision,
                )
            if metadata.id != session_id:
                raise CorruptSessionError(session_id, "updated metadata identity changed")
            updated = SessionMetadataEnvelope(
                revision=current.revision + 1,
                committed_entry_count=current.committed_entry_count,
                metadata=metadata,
            )
            self._validate_loaded_entries(updated, list(current_state.entries))
            await asyncio.to_thread(self._atomic_write_metadata, updated)
            return updated

    async def append_entries(
        self,
        session_id: str,
        *,
        expected_revision: int,
        entries: tuple[Entry, ...],
        metadata: SessionMetadata,
    ) -> LoadedSession:
        """追加一批 Entries，并以新的元数据 revision 原子确认提交。

        新 Entries 先进入 journal，随后 metadata 的 ``committed_entry_count`` 才成为
        提交点；崩溃遗留的尾部记录会在读取时作为未提交后缀忽略。
        """

        if not entries:
            raise ValueError("append_entries requires at least one Entry")
        async with self._lock(session_id):
            current = await self.load_state(session_id)
            if current.envelope.revision != expected_revision:
                raise StaleSessionHandleError(
                    session_id,
                    expected_revision,
                    current.envelope.revision,
                )
            if metadata.id != session_id:
                raise CorruptSessionError(session_id, "updated metadata identity changed")
            self._validate_append(current, entries, metadata.leaf_id)
            updated = SessionMetadataEnvelope(
                revision=current.envelope.revision + 1,
                committed_entry_count=current.envelope.committed_entry_count + len(entries),
                metadata=metadata,
            )
            await asyncio.to_thread(
                self._append_and_commit_sync,
                updated,
                entries,
                bool(current.diagnostics),
            )
            return LoadedSession(
                envelope=updated,
                entries=(*current.entries, *entries),
                diagnostics=current.diagnostics,
            )

    def _lock(self, session_id: str) -> asyncio.Lock:
        """取得当前进程内此 Session 唯一的异步写锁。"""

        key = (self.root.absolute(), session_id)
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[key] = lock
        return lock

    def _create_sync(self, envelope: SessionMetadataEnvelope) -> None:
        """在线程中以“空 journal + 原子 metadata”创建文件对。"""

        self.root.mkdir(parents=True, exist_ok=True)
        journal = self.journal_path(envelope.metadata.id)
        metadata = self.metadata_path(envelope.metadata.id)
        if journal.exists() or metadata.exists():
            raise CorruptSessionError(envelope.metadata.id, "generated identity already exists")
        journal_created = False
        try:
            with journal.open("x", encoding="utf-8") as file:
                journal_created = True
                file.flush()
                os.fsync(file.fileno())
            self._atomic_write_metadata(envelope)
        except BaseException:
            if journal_created:
                metadata.unlink(missing_ok=True)
                journal.unlink(missing_ok=True)
            raise

    def _rollback_empty_subagent_sync(self, session_id: str) -> None:
        """删除已确认为空的 Subagent Session 全部磁盘产品。"""

        self.trace_path(session_id).unlink(missing_ok=True)
        self.metadata_path(session_id).unlink()
        self.journal_path(session_id).unlink()

    def _remove_session_files_sync(self, session_id: str) -> None:
        """尽力清理一次失败创建可能留下的 Session 文件。"""

        self.trace_path(session_id).unlink(missing_ok=True)
        self.metadata_path(session_id).unlink(missing_ok=True)
        self.journal_path(session_id).unlink(missing_ok=True)

    def _load_sync(self, session_id: str) -> SessionMetadataEnvelope:
        """读取并校验 metadata 信封以及 journal 的存在性。"""

        path = self.metadata_path(session_id)
        if not path.is_file():
            raise SessionNotFoundError(session_id)
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CorruptSessionError(session_id, "metadata is not valid UTF-8 JSON") from error
        if not isinstance(raw, dict):
            raise CorruptSessionError(session_id, "metadata envelope must be an object")
        version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            raise IncompatibleSessionVersionError(session_id, version)
        try:
            envelope = SessionMetadataEnvelope.model_validate(raw)
        except ValidationError as error:
            raise CorruptSessionError(session_id, "metadata validation failed") from error
        if envelope.metadata.id != session_id:
            raise CorruptSessionError(session_id, "metadata identity does not match its filename")
        if not self.journal_path(session_id).is_file():
            raise CorruptSessionError(session_id, "Entry journal is missing")
        return envelope

    def _load_state_sync(self, session_id: str) -> LoadedSession:
        """只解析 metadata 已确认提交的 journal 前缀。"""

        envelope = self._load_sync(session_id)
        path = self.journal_path(session_id)
        try:
            lines = path.read_bytes().splitlines(keepends=True)
        except OSError as error:
            raise CorruptSessionError(session_id, "Entry journal could not be read") from error
        committed = envelope.committed_entry_count
        # 少于提交数意味着 metadata 指向不存在的数据，不能静默恢复。
        if len(lines) < committed:
            raise CorruptSessionError(
                session_id,
                "Entry journal contains fewer records than metadata commits",
            )
        entries: list[Entry] = []
        for index, line in enumerate(lines[:committed]):
            # 每条已提交记录必须以换行结束；否则可能是崩溃时的半条写入。
            if not line.endswith(b"\n"):
                raise CorruptSessionError(
                    session_id,
                    f"committed Entry record {index} is incomplete",
                )
            try:
                raw: Any = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise CorruptSessionError(
                    session_id,
                    f"committed Entry record {index} is invalid JSON",
                ) from error
            if not isinstance(raw, dict):
                raise CorruptSessionError(
                    session_id,
                    f"committed Entry record {index} must be an object",
                )
            version = raw.get("schema_version")
            if version != SCHEMA_VERSION:
                raise IncompatibleSessionVersionError(session_id, version)
            try:
                entries.append(parse_entry(raw))
            except ValidationError as error:
                raise CorruptSessionError(
                    session_id,
                    f"committed Entry record {index} failed validation",
                ) from error
        diagnostics = ()
        if len(lines) > committed:
            # journal 先写、metadata 后提交；多出的完整或残缺尾部都不是对话状态。
            diagnostics = (
                f"ignored {len(lines) - committed} uncommitted trailing Entry record(s)",
            )
        self._validate_loaded_entries(envelope, entries)
        return LoadedSession(envelope, tuple(entries), diagnostics)

    def _list_metadata_sync(self) -> list[SessionMetadataEnvelope]:
        """同步扫描并稳定排序 metadata 文件。"""

        if not self.root.exists():
            return []
        envelopes = [
            self._load_sync(path.name.removesuffix(".metadata.json"))
            for path in self.root.glob("*.metadata.json")
        ]
        return sorted(envelopes, key=lambda item: (item.metadata.created_at, item.metadata.id))

    def _atomic_write_metadata(self, envelope: SessionMetadataEnvelope) -> None:
        """用临时文件、fsync 与 replace 原子提交 metadata。"""

        self.root.mkdir(parents=True, exist_ok=True)
        target = self.metadata_path(envelope.metadata.id)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        payload = envelope.model_dump_json(indent=2) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
            # 文件落盘后同步目录项，保证 rename 在系统崩溃后也持久。
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _append_and_commit_sync(
        self,
        envelope: SessionMetadataEnvelope,
        entries: tuple[Entry, ...],
        preserve_uncommitted_suffix: bool,
    ) -> None:
        """先持久化新 Entries，再推进 metadata 中的提交边界。"""

        path = self.journal_path(envelope.metadata.id)
        serialized = "".join(f"{dump_entry(entry)}\n" for entry in entries).encode()
        if preserve_uncommitted_suffix:
            # 已发现崩溃尾部时，把新提交插入已提交前缀之后，并保留旧尾部供诊断。
            previous_committed = envelope.committed_entry_count - len(entries)
            lines = path.read_bytes().splitlines(keepends=True)
            committed_prefix = b"".join(lines[:previous_committed])
            uncommitted_suffix = b"".join(lines[previous_committed:])
            self._atomic_write_journal(
                path,
                committed_prefix + serialized + uncommitted_suffix,
            )
        else:
            # 正常路径只需追加，避免为每次消息重写整个 Session journal。
            with path.open("ab") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        self._atomic_write_metadata(envelope)

    def _atomic_write_journal(self, target: Path, payload: bytes) -> None:
        """在需要重排未提交后缀时原子重写完整 journal。"""

        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _validate_append(
        current: LoadedSession,
        entries: tuple[Entry, ...],
        leaf_id: str | None,
    ) -> None:
        """验证新增节点能接入既有 Entry 树且新 leaf 已提交。"""

        known = {entry.id for entry in current.entries}
        allowed_external = current.envelope.metadata.fork_point_entry_id
        for entry in entries:
            if entry.id in known:
                raise CorruptSessionError(current.envelope.metadata.id, "duplicate Entry ID")
            if entry.parent_id is not None and (
                entry.parent_id not in known and entry.parent_id != allowed_external
            ):
                raise CorruptSessionError(
                    current.envelope.metadata.id,
                    f"new Entry {entry.id!r} has unavailable parent {entry.parent_id!r}",
                )
            if entry.parent_id is None and (known or current.envelope.metadata.origin == "fork"):
                # 一个 Session 只能有一个本地根；Fork 的根位于父 Session 中。
                raise CorruptSessionError(
                    current.envelope.metadata.id,
                    "new Entry would create a second conversation root",
                )
            known.add(entry.id)
        if leaf_id not in known:
            raise CorruptSessionError(
                current.envelope.metadata.id,
                "updated leaf does not reference a committed Entry",
            )

    @staticmethod
    def _validate_loaded_entries(
        envelope: SessionMetadataEnvelope,
        entries: list[Entry],
    ) -> None:
        """验证完整已提交 Entry 集合的引用、无环性、根与 leaf 不变量。"""

        session_id = envelope.metadata.id
        by_id = {entry.id: entry for entry in entries}
        if len(by_id) != len(entries):
            raise CorruptSessionError(session_id, "Entry IDs are not unique")
        allowed_external = envelope.metadata.fork_point_entry_id
        for entry in entries:
            if entry.parent_id is not None and (
                entry.parent_id not in by_id and entry.parent_id != allowed_external
            ):
                raise MissingEntryParentError(
                    session_id,
                    entry.id,
                    entry.parent_id,
                )
        resolved: set[str] = set()
        for entry in entries:
            # 从每个未解析节点沿 parent_id 上溯；局部 trail 用于检测树中的环。
            trail: set[str] = set()
            current: Entry | None = entry
            while current is not None and current.id not in resolved:
                if current.id in trail:
                    raise SessionLineageCycleError(session_id)
                trail.add(current.id)
                current = by_id.get(current.parent_id) if current.parent_id is not None else None
            resolved.update(trail)
        # 普通/Subagent Session 恰有一个本地根；Fork 通过外部 fork point 接根。
        root_count = sum(entry.parent_id is None for entry in entries)
        if envelope.metadata.origin == "fork" and root_count:
            raise CorruptSessionError(session_id, "fork journal contains a local root")
        if envelope.metadata.origin != "fork" and entries and root_count != 1:
            raise CorruptSessionError(session_id, "Entry journal must contain exactly one root")
        leaf_id = envelope.metadata.leaf_id
        if leaf_id is None:
            if entries:
                raise CorruptSessionError(session_id, "non-empty journal has no current leaf")
        elif leaf_id not in by_id and leaf_id != allowed_external:
            raise InvalidSessionLeafError(session_id, leaf_id)
