from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """phi's model-provider config — an OpenAI-compatible litellm proxy."""

    model_config = SettingsConfigDict(
        env_prefix="PHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str = "https://ai.ukehome.top/v1"
    api_key: SecretStr = SecretStr("")
    default_model: str = ""
    request_timeout_seconds: float = 180.0
