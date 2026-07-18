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

_SESSION_LOCKS: dict[tuple[Path, str], asyncio.Lock] = {}


@dataclass(frozen=True)
class LoadedSession:
    envelope: SessionMetadataEnvelope
    entries: tuple[Entry, ...]
    diagnostics: tuple[str, ...] = ()


class SessionStorage:
    """Versioned on-disk Session storage rooted outside the workspace by default."""

    def __init__(self, root: Path | str = Path("~/.phi/sessions")) -> None:
        self.root = Path(root).expanduser()

    def journal_path(self, session_id: str) -> Path:
        return self._session_path(session_id, ".jsonl")

    def metadata_path(self, session_id: str) -> Path:
        return self._session_path(session_id, ".metadata.json")

    def trace_path(self, session_id: str) -> Path:
        return self._session_path(session_id, ".trace.jsonl")

    def _session_path(self, session_id: str, suffix: str) -> Path:
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
        await asyncio.to_thread(self._create_sync, envelope)
        return envelope

    async def load(self, session_id: str) -> SessionMetadataEnvelope:
        return await asyncio.to_thread(self._load_sync, session_id)

    async def load_state(self, session_id: str) -> LoadedSession:
        return await asyncio.to_thread(self._load_state_sync, session_id)

    async def list_metadata(self) -> list[SessionMetadataEnvelope]:
        return await asyncio.to_thread(self._list_metadata_sync)

    async def replace_metadata(
        self,
        session_id: str,
        *,
        expected_revision: int,
        metadata: SessionMetadata,
    ) -> SessionMetadataEnvelope:
        async with self._lock(session_id):
            current_state = await self.load_state(session_id)
            current = current_state.envelope
            if current.revision != expected_revision:
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
        key = (self.root.absolute(), session_id)
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[key] = lock
        return lock

    def _create_sync(self, envelope: SessionMetadataEnvelope) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        journal = self.journal_path(envelope.metadata.id)
        metadata = self.metadata_path(envelope.metadata.id)
        if journal.exists() or metadata.exists():
            raise CorruptSessionError(envelope.metadata.id, "generated identity already exists")
        with journal.open("x", encoding="utf-8") as file:
            file.flush()
            os.fsync(file.fileno())
        self._atomic_write_metadata(envelope)

    def _load_sync(self, session_id: str) -> SessionMetadataEnvelope:
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
        envelope = self._load_sync(session_id)
        path = self.journal_path(session_id)
        try:
            lines = path.read_bytes().splitlines(keepends=True)
        except OSError as error:
            raise CorruptSessionError(session_id, "Entry journal could not be read") from error
        committed = envelope.committed_entry_count
        if len(lines) < committed:
            raise CorruptSessionError(
                session_id,
                "Entry journal contains fewer records than metadata commits",
            )
        entries: list[Entry] = []
        for index, line in enumerate(lines[:committed]):
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
            diagnostics = (
                f"ignored {len(lines) - committed} uncommitted trailing Entry record(s)",
            )
        self._validate_loaded_entries(envelope, entries)
        return LoadedSession(envelope, tuple(entries), diagnostics)

    def _list_metadata_sync(self) -> list[SessionMetadataEnvelope]:
        if not self.root.exists():
            return []
        envelopes = [
            self._load_sync(path.name.removesuffix(".metadata.json"))
            for path in self.root.glob("*.metadata.json")
        ]
        return sorted(envelopes, key=lambda item: (item.metadata.created_at, item.metadata.id))

    def _atomic_write_metadata(self, envelope: SessionMetadataEnvelope) -> None:
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
        path = self.journal_path(envelope.metadata.id)
        serialized = "".join(f"{dump_entry(entry)}\n" for entry in entries).encode()
        if preserve_uncommitted_suffix:
            previous_committed = envelope.committed_entry_count - len(entries)
            lines = path.read_bytes().splitlines(keepends=True)
            committed_prefix = b"".join(lines[:previous_committed])
            uncommitted_suffix = b"".join(lines[previous_committed:])
            self._atomic_write_journal(
                path,
                committed_prefix + serialized + uncommitted_suffix,
            )
        else:
            with path.open("ab") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        self._atomic_write_metadata(envelope)

    def _atomic_write_journal(self, target: Path, payload: bytes) -> None:
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
            trail: set[str] = set()
            current: Entry | None = entry
            while current is not None and current.id not in resolved:
                if current.id in trail:
                    raise SessionLineageCycleError(session_id)
                trail.add(current.id)
                current = by_id.get(current.parent_id) if current.parent_id is not None else None
            resolved.update(trail)
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
