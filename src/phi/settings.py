from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from phi.harness.compaction import CompactionSettings


class Settings(BaseSettings):
    """phi's model-provider config — an OpenAI-compatible litellm proxy."""

    model_config = SettingsConfigDict(
        env_prefix="PHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    base_url: str = "https://ai.ukehome.top/v1"
    api_key: SecretStr = SecretStr("")
    default_model: str = ""
    request_timeout_seconds: float = 180.0
    session_dir: Path = Path("~/.phi/sessions").expanduser()
    compaction_enabled: bool = True
    compaction_reserve_tokens: int = Field(default=16_384, ge=0)
    compaction_keep_recent_tokens: int = Field(default=20_000, ge=0)
    compaction_summary_max_tokens: int = Field(default=4_096, gt=0)
    compaction_max_input_tokens: int | None = Field(default=None, gt=0)

    @property
    def compaction(self) -> CompactionSettings:
        return CompactionSettings(
            enabled=self.compaction_enabled,
            reserve_tokens=self.compaction_reserve_tokens,
            keep_recent_tokens=self.compaction_keep_recent_tokens,
            summary_max_tokens=self.compaction_summary_max_tokens,
            max_input_tokens=self.compaction_max_input_tokens,
        )
