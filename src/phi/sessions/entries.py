from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from phi.model import ToolCall, ToolResult
from phi.sessions.metadata import SCHEMA_VERSION


def _entry_id() -> str:
    return str(uuid4())


def _timestamp() -> datetime:
    return datetime.now(UTC)


class EntryBase(BaseModel):
    """Shared validated fields for one durable conversation Entry."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(default_factory=_entry_id, min_length=1)
    parent_id: str | None = None
    created_at: datetime = Field(default_factory=_timestamp)

    @field_validator("parent_id")
    @classmethod
    def parent_id_must_not_be_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Entry parent IDs must be non-empty when supplied")
        return value

    @field_validator("id")
    @classmethod
    def id_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Entry IDs must be non-empty")
        return value

    @field_validator("created_at")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Entry timestamps must be timezone-aware")
        return value


class UserMessageEntry(EntryBase):
    entry_type: Literal["user_message"] = "user_message"
    content: str = Field(min_length=1)


class AssistantMessageEntry(EntryBase):
    entry_type: Literal["assistant_message"] = "assistant_message"
    content: str | None = None
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @field_validator("tool_calls")
    @classmethod
    def validate_tool_calls(cls, calls: tuple[ToolCall, ...]) -> tuple[ToolCall, ...]:
        seen: set[str] = set()
        for call in calls:
            if not call.id or not call.name:
                raise ValueError("Tool Calls require non-empty IDs and names")
            if call.id in seen:
                raise ValueError("Tool Call IDs must be unique within an Assistant message")
            seen.add(call.id)
        return calls

    @model_validator(mode="after")
    def response_has_model_output(self) -> AssistantMessageEntry:
        if self.content is None and self.reasoning is None and not self.tool_calls:
            raise ValueError("Assistant Entries require content, reasoning, or Tool Calls")
        return self


class ToolResultEntry(EntryBase):
    entry_type: Literal["tool_result"] = "tool_result"
    result: ToolResult

    @field_validator("result")
    @classmethod
    def result_has_call_id(cls, value: ToolResult) -> ToolResult:
        if not value.call_id:
            raise ValueError("Tool Results require a non-empty Tool Call ID")
        return value


class CompactionEntry(EntryBase):
    entry_type: Literal["compaction"] = "compaction"
    summary: str = Field(min_length=1)
    tokens_before: int = Field(ge=0)
    tokens_before_source: Literal["provider", "estimate"]
    first_kept_entry_id: str = Field(min_length=1)

    @field_validator("summary", "first_kept_entry_id")
    @classmethod
    def compaction_text_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Compaction text fields must be non-empty")
        return value


type Entry = Annotated[
    UserMessageEntry | AssistantMessageEntry | ToolResultEntry | CompactionEntry,
    Field(discriminator="entry_type"),
]

ENTRY_ADAPTER = TypeAdapter(Entry)


def parse_entry(raw: Any) -> Entry:
    return ENTRY_ADAPTER.validate_python(raw)


def dump_entry(entry: Entry) -> str:
    return ENTRY_ADAPTER.dump_json(entry).decode("utf-8")
