"""定义 Session 对话树中可持久化的 Entry 变体及其序列化边界。"""

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
    """为新 Entry 生成全局唯一标识。"""

    return str(uuid4())


def _timestamp() -> datetime:
    """生成带 UTC 时区的持久化时间戳。"""

    return datetime.now(UTC)


class EntryBase(BaseModel):
    """一个持久化对话 Entry 共享的、经过校验的字段。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(default_factory=_entry_id, min_length=1)
    parent_id: str | None = None
    created_at: datetime = Field(default_factory=_timestamp)

    @field_validator("parent_id")
    @classmethod
    def parent_id_must_not_be_empty(cls, value: str | None) -> str | None:
        """禁止用空白字符串伪装“存在的父节点”。"""

        if value is not None and not value.strip():
            raise ValueError("Entry parent IDs must be non-empty when supplied")
        return value

    @field_validator("id")
    @classmethod
    def id_must_not_be_whitespace(cls, value: str) -> str:
        """确保 Entry ID 能作为树节点的稳定索引键。"""

        if not value.strip():
            raise ValueError("Entry IDs must be non-empty")
        return value

    @field_validator("created_at")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        """拒绝无法跨时区可靠解释的朴素时间。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Entry timestamps must be timezone-aware")
        return value


class UserMessageEntry(EntryBase):
    """记录用户提交给 Session 的一条持久消息。"""

    entry_type: Literal["user_message"] = "user_message"
    content: str = Field(min_length=1)


class AssistantMessageEntry(EntryBase):
    """记录 Model 输出的文本、推理内容与 Tool Call。"""

    entry_type: Literal["assistant_message"] = "assistant_message"
    content: str | None = None
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @field_validator("tool_calls")
    @classmethod
    def validate_tool_calls(cls, calls: tuple[ToolCall, ...]) -> tuple[ToolCall, ...]:
        """校验同一 Assistant 消息内 Tool Call 的可关联性与唯一性。"""

        # Tool Result 之后通过 call_id 回连 Tool Call；重复 ID 会让关联产生歧义。
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
        """确保 Assistant Entry 至少保存一种实际的 Model 输出。"""

        if self.content is None and self.reasoning is None and not self.tool_calls:
            raise ValueError("Assistant Entries require content, reasoning, or Tool Calls")
        return self


class ToolResultEntry(EntryBase):
    """记录 Harness 处理一个 Tool Call 后得到的 Tool Result。"""

    entry_type: Literal["tool_result"] = "tool_result"
    result: ToolResult

    @field_validator("result")
    @classmethod
    def result_has_call_id(cls, value: ToolResult) -> ToolResult:
        """确保 Tool Result 可以回连产生它的 Tool Call。"""

        if not value.call_id:
            raise ValueError("Tool Results require a non-empty Tool Call ID")
        return value


class CompactionEntry(EntryBase):
    """用摘要替代 Conversation View 中较旧的 Entries，但不删除原节点。"""

    entry_type: Literal["compaction"] = "compaction"
    summary: str = Field(min_length=1)
    tokens_before: int = Field(ge=0)
    tokens_before_source: Literal["provider", "estimate"]
    first_kept_entry_id: str = Field(min_length=1)

    @field_validator("summary", "first_kept_entry_id")
    @classmethod
    def compaction_text_must_not_be_whitespace(cls, value: str) -> str:
        """确保摘要及保留边界都携带有效内容。"""

        if not value.strip():
            raise ValueError("Compaction text fields must be non-empty")
        return value


type Entry = Annotated[
    UserMessageEntry | AssistantMessageEntry | ToolResultEntry | CompactionEntry,
    Field(discriminator="entry_type"),
]

# 显式 discriminator 避免从字段形状猜测 Entry 变体，保证磁盘格式可演进。
ENTRY_ADAPTER = TypeAdapter(Entry)


def parse_entry(raw: Any) -> Entry:
    """在不可信磁盘数据边界解析并校验一个 Entry。"""

    return ENTRY_ADAPTER.validate_python(raw)


def dump_entry(entry: Entry) -> str:
    """把已校验 Entry 编码成单条 UTF-8 JSONL 记录。"""

    return ENTRY_ADAPTER.dump_json(entry).decode("utf-8")
