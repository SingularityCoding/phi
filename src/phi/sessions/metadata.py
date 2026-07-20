"""定义 Session 身份、分支谱系与提交状态的持久化元数据。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1


class SessionMetadata(BaseModel):
    """一个 Session 经过校验的持久身份与分支元数据。"""

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
        """确保跨进程读取时仍能唯一解释时间点。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Session timestamps must be timezone-aware")
        return value

    @field_validator("id")
    @classmethod
    def id_must_not_be_whitespace(cls, value: str) -> str:
        """确保 Session ID 可安全用于文件名与索引。"""

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
        """用 ``None`` 表示缺失，禁止含义模糊的空字符串。"""

        if value is not None and not value.strip():
            raise ValueError("optional Session strings must be non-empty when supplied")
        return value

    @model_validator(mode="after")
    def lineage_matches_origin(self) -> SessionMetadata:
        """强制 ``origin`` 与 Fork/Subagent 谱系字段保持一致。"""

        # 新 Session 没有父历史；Fork 必须精确引用父 Session 的分叉点。
        if self.origin == "new" and (
            self.parent_session_id is not None or self.fork_point_entry_id is not None
        ):
            raise ValueError("new Sessions cannot contain parent lineage")
        if self.origin == "fork" and (
            self.parent_session_id is None or self.fork_point_entry_id is None
        ):
            raise ValueError("fork Sessions require a parent Session and fork point")
        # Subagent 的父链接只记录 Delegation 谱系，不能继承父 Conversation View。
        if self.origin == "subagent" and (
            self.parent_session_id is None or self.fork_point_entry_id is not None
        ):
            raise ValueError("Subagent Sessions require parent lineage without a fork point")
        return self


class SessionMetadataEnvelope(BaseModel):
    """带版本并可独立原子提交的 Session 元数据文档。"""

    # revision 用于乐观并发控制；committed_entry_count 界定 journal 的已提交前缀。

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    revision: int = Field(ge=0)
    committed_entry_count: int = Field(ge=0)
    metadata: SessionMetadata
