"""Runtime settings read from the environment contract (`infrastructure/env/.env.example`).

Secrets and connection strings (which embed credentials) are `SecretStr`, so they never show up
in `repr()`, logs or validation errors.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    UKOS_ENV: Literal["local", "test", "ci"] = "local"
    UKOS_LOG_LEVEL: Literal["debug", "info", "warning", "error"] = "info"

    DATABASE_URL: SecretStr = SecretStr("postgresql+psycopg://ukos_app@127.0.0.1:5532/ukos")
    # Admin-role connection used only by Alembic (never by the request path; RLS bypass risk).
    DATABASE_MIGRATION_URL: SecretStr = SecretStr(
        "postgresql+psycopg://ukos_admin@127.0.0.1:5532/ukos"
    )
    REDIS_URL: SecretStr = SecretStr("redis://127.0.0.1:6479/0")

    # OIDC bearer-token validation (contract docs/architecture/m01-contracts.md section 1).
    # Tokens carry the public issuer; JWKS is fetched from the compose-internal URL.
    OIDC_ISSUER_URL: str = "http://127.0.0.1:8380/dex"
    OIDC_INTERNAL_URL: str = "http://127.0.0.1:8380/dex"
    OIDC_AUDIENCE: str = "ukos-api"
    OIDC_JWKS_CACHE_SECONDS: int = Field(default=300, ge=0, le=86_400)
    OIDC_LEEWAY_SECONDS: int = Field(default=30, ge=0, le=30)

    # Comma-separated list of allowed CORS origins.
    API_CORS_ORIGINS: str = ""
    API_SECRET_KEY: SecretStr | None = None

    # Uploads and quotas (contract docs/architecture/m02-contracts.md §3, §5).
    UPLOAD_MAX_BYTES: int = Field(default=104_857_600, ge=1, le=5 * 1024**3)  # 100 MiB
    ORG_STORAGE_QUOTA_BYTES: int = Field(default=5_368_709_120, ge=1, le=1024**5)  # 5 GiB
    UPLOAD_SESSION_TTL_SECONDS: int = Field(default=3600, ge=1, le=7 * 86_400)
    UPLOAD_MAX_PENDING_PER_USER: int = Field(default=20, ge=1, le=1000)
    DOCUMENT_PURGE_GRACE_DAYS: int = Field(default=7, ge=1, le=3650)

    # Per-dependency timeout for /health/ready, seconds.
    HEALTH_CHECK_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=30)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.API_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def psycopg_dsn(self) -> str:
        """DATABASE_URL as a libpq URI (drops the SQLAlchemy `+psycopg` driver suffix)."""
        return to_libpq_dsn(self.DATABASE_URL.get_secret_value())

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL with the psycopg 3 driver forced (``postgresql+psycopg://``)."""
        return to_sqlalchemy_url(self.DATABASE_URL.get_secret_value())

    @property
    def sqlalchemy_migration_url(self) -> str:
        """DATABASE_MIGRATION_URL with the psycopg 3 driver forced."""
        return to_sqlalchemy_url(self.DATABASE_MIGRATION_URL.get_secret_value())


def to_libpq_dsn(url: str) -> str:
    """Drop a SQLAlchemy ``+driver`` suffix so libpq/psycopg accept the URI."""
    scheme, sep, rest = url.partition("://")
    if sep and "+" in scheme:
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}{sep}{rest}"


def to_sqlalchemy_url(url: str) -> str:
    """Normalize ``postgres://``, ``postgresql://`` or ``postgresql+x://`` to psycopg 3."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    base = scheme.split("+", 1)[0]
    if base in ("postgres", "postgresql"):
        return f"postgresql+psycopg://{rest}"
    return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
