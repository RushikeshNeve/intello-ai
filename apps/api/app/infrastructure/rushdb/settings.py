"""RushDB settings: ``RUSHDB_URL`` and ``RUSHDB_TOKEN`` (contract m03 section 8).

Separate from ``app.core.config.Settings`` on purpose: the adapter owns its configuration, and only
the API/worker containers receive these variables. The token is a ``SecretStr`` so it never appears
in ``repr()``, logs or validation errors.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RushDbSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=True, hide_input_in_errors=True
    )

    # In compose: http://rushdb:3000.
    RUSHDB_URL: str = "http://127.0.0.1:3310"
    RUSHDB_TOKEN: SecretStr = Field(min_length=1)

    # Fail fast so the API can answer 503 instead of hanging.
    RUSHDB_CONNECT_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0, le=60)
    RUSHDB_READ_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=300)
    # Records written per batch (writes are one request per record; a batch bounds a retry unit).
    RUSHDB_BATCH_SIZE: int = Field(default=100, ge=1, le=100)
    # Total attempts for idempotent calls on transient failures (1 = no retry).
    RUSHDB_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=3)
    RUSHDB_RETRY_BACKOFF_SECONDS: float = Field(default=0.1, ge=0, le=5)
    RUSHDB_INDEX_READY_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=600)

    @field_validator("RUSHDB_URL")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http(s) URL with a host")
        if parts.username or parts.password:
            raise ValueError("must not embed credentials")
        if parts.query or parts.fragment or parts.path not in ("", "/"):
            raise ValueError("must be an origin without path, query or fragment")
        return f"{parts.scheme}://{parts.netloc}"
