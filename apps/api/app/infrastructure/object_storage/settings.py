"""Object-store settings: the existing ``S3_*`` environment variables (contract section 2).

Separate from ``app.core.config.Settings`` on purpose: the adapter owns its configuration.
Credentials are ``SecretStr`` so they never appear in ``repr()``, logs or validation errors.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


def _validate_endpoint(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http(s) URL with a host")
    if parts.username or parts.password:
        raise ValueError("must not embed credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("must be an origin without path, query or fragment")
    return f"{parts.scheme}://{parts.netloc}"


class ObjectStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=True, hide_input_in_errors=True
    )

    # Server-side calls (head/get/put/delete). In compose: http://objectstore:9000.
    S3_ENDPOINT_URL: str = "http://127.0.0.1:9300"
    # The origin the BROWSER uses; presigned URLs are signed against it (the host is signed).
    S3_PUBLIC_ENDPOINT_URL: str = "http://127.0.0.1:9300"
    S3_REGION: str = Field(default="us-east-1", min_length=1, max_length=64)
    S3_ACCESS_KEY_ID: SecretStr = Field(min_length=1)
    S3_SECRET_ACCESS_KEY: SecretStr = Field(min_length=1)
    S3_BUCKET_ORIGINALS: str = "ukos-originals"
    S3_BUCKET_ARTIFACTS: str = "ukos-artifacts"
    S3_FORCE_PATH_STYLE: bool = True
    S3_PRESIGN_TTL_SECONDS: int = Field(default=300, ge=1, le=7 * 24 * 3600)

    # Short, bounded client behaviour: fail fast so the API can answer 503 instead of hanging.
    S3_CONNECT_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0, le=60)
    S3_READ_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=300)
    S3_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=5)

    @field_validator("S3_ENDPOINT_URL", "S3_PUBLIC_ENDPOINT_URL")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        return _validate_endpoint(value)

    @field_validator("S3_BUCKET_ORIGINALS", "S3_BUCKET_ARTIFACTS")
    @classmethod
    def _bucket(cls, value: str) -> str:
        if not _BUCKET_RE.fullmatch(value) or ".." in value:
            raise ValueError(
                "must be a valid S3 bucket name (3-63 chars, lower-case, digits, . and -)"
            )
        return value
