"""Chat/verification settings: this adapter's OWN settings class over the shared env names
(contract section 11). ``AI_API_KEY`` and ``OPENAI_API_KEY`` are ``SecretStr``: never in
``repr()``, logs or errors."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChatSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=True, hide_input_in_errors=True
    )

    AI_BASE_URL: str = "http://host.docker.internal:1234/v1"
    AI_API_KEY: SecretStr | None = None
    AI_EXTERNAL_PROVIDERS_ENABLED: bool = False

    CHAT_MODEL: str = Field(default="google/gemma-3-4b", min_length=1)
    CHAT_CONTEXT_TOKENS: int = Field(default=4096, ge=256, le=2_000_000)
    CHAT_MAX_OUTPUT_TOKENS: int = Field(default=800, ge=1, le=100_000)
    CHAT_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0)
    CHAT_HISTORY_TURNS: int = Field(default=6, ge=0, le=50)
    # Read timeout between chunks (a first call may JIT-load the model) and a whole-generation cap.
    CHAT_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=1800)
    CHAT_CONNECT_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=60)
    CHAT_GENERATION_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=3600)
    CHAT_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)

    VERIFY_MODE: Literal["strict", "flag"] = "strict"
    VERIFY_LEXICAL_MIN: float = Field(default=0.35, ge=0.0, le=1.0)
    VERIFY_EMBED_MIN: float = Field(default=0.55, ge=-1.0, le=1.0)

    OPENAI_API_KEY: SecretStr | None = None
    OPENAI_CHAT_MODEL: str = Field(default="gpt-4o-mini", min_length=1)
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_CONTEXT_TOKENS: int = Field(default=128_000, ge=256, le=2_000_000)

    @field_validator("AI_API_KEY", "OPENAI_API_KEY", mode="before")
    @classmethod
    def _blank_or_placeholder_is_none(cls, value: object) -> object:
        # An unset key, an empty one and the ``change-me-*`` placeholder of .env.example mean "no key".  # noqa: E501
        if isinstance(value, str) and (not value.strip() or value.strip().startswith("change-me")):
            return None
        return value

    def openai_key(self) -> str | None:
        return self.OPENAI_API_KEY.get_secret_value() if self.OPENAI_API_KEY else None
