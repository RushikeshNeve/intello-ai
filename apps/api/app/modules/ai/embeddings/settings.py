"""Embedding adapter settings: its OWN settings class over the shared ``AI_*`` env names.

Separate from ``app.core.config.Settings`` on purpose (the adapter owns its configuration, like the
object store). The API key is a ``SecretStr`` so it never appears in ``repr()``, logs or errors.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.modules.ai.embeddings.registry import (
    DEFAULT_DIMENSIONS,
    DEFAULT_DOCUMENT_PREFIX,
    DEFAULT_MAX_INPUT_CHARS,
    DEFAULT_MODEL_ID,
    DEFAULT_QUERY_PREFIX,
    DEFAULT_SPEC_VERSION,
    MAX_DIMENSIONS,
)


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=True, hide_input_in_errors=True
    )

    # Inside containers: http://host.docker.internal:1234/v1 (LM Studio on the host).
    AI_BASE_URL: str = "http://host.docker.internal:1234/v1"
    AI_API_KEY: SecretStr | None = None
    AI_EXTERNAL_PROVIDERS_ENABLED: bool = False

    AI_EMBEDDING_MODEL: str = Field(default=DEFAULT_MODEL_ID, min_length=1)
    AI_EMBEDDING_DIMENSIONS: int = Field(default=DEFAULT_DIMENSIONS, ge=1, le=MAX_DIMENSIONS)
    AI_EMBEDDING_QUERY_PREFIX: str = DEFAULT_QUERY_PREFIX
    AI_EMBEDDING_DOCUMENT_PREFIX: str = DEFAULT_DOCUMENT_PREFIX
    AI_EMBEDDING_SPEC_VERSION: int = Field(default=DEFAULT_SPEC_VERSION, ge=1)
    AI_EMBEDDING_MAX_INPUT_CHARS: int = Field(default=DEFAULT_MAX_INPUT_CHARS, ge=1, le=1_000_000)
    AI_EMBEDDING_NORMALIZE: bool = True

    # A first call may JIT-load the model in LM Studio, hence the generous read timeout.
    AI_EMBEDDING_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=600)
    AI_EMBEDDING_CONNECT_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=60)
    AI_EMBEDDING_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    INGEST_EMBED_BATCH_SIZE: int = Field(default=16, ge=1, le=512)

    @field_validator("AI_API_KEY", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value
