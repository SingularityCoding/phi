from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1


class SessionMetadata(BaseModel):
    """Validated durable identity and branch metadata for one Session."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    leaf_id: str | None = None
    parent_session_id: str | None = None
    fork_point_entry_id: str | None = None
    name: str | None = None
    model: str | None = None
    origin: Literal["new", "fork", "subagent"] = "new"

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Session timestamps must be timezone-aware")
        return value

    @field_validator("id")
    @classmethod
    def id_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Session IDs must be non-empty")
        return value

    @field_validator(
        "leaf_id",
        "parent_session_id",
        "fork_point_entry_id",
        "name",
        "model",
    )
    @classmethod
    def optional_strings_must_not_be_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("optional Session strings must be non-empty when supplied")
        return value

    @model_validator(mode="after")
    def lineage_matches_origin(self) -> SessionMetadata:
        if self.origin == "new" and (
            self.parent_session_id is not None or self.fork_point_entry_id is not None
        ):
            raise ValueError("new Sessions cannot contain parent lineage")
        if self.origin == "fork" and (
            self.parent_session_id is None or self.fork_point_entry_id is None
        ):
            raise ValueError("fork Sessions require a parent Session and fork point")
        if self.origin == "subagent" and (
            self.parent_session_id is None or self.fork_point_entry_id is not None
        ):
            raise ValueError("Subagent Sessions require parent lineage without a fork point")
        return self


class SessionMetadataEnvelope(BaseModel):
    """Versioned, independently committed Session metadata document."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    revision: int = Field(ge=0)
    committed_entry_count: int = Field(ge=0)
    metadata: SessionMetadata
