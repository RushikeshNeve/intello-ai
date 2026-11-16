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
    REDIS_URL: SecretStr = SecretStr("redis://127.0.0.1:6479/0")

    # Comma-separated list of allowed CORS origins.
    API_CORS_ORIGINS: str = ""
    API_SECRET_KEY: SecretStr | None = None

    # Per-dependency timeout for /health/ready, seconds.
    HEALTH_CHECK_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=30)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.API_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def psycopg_dsn(self) -> str:
        """DATABASE_URL as a libpq URI (drops the SQLAlchemy `+psycopg` driver suffix)."""
        url = self.DATABASE_URL.get_secret_value()
        scheme, sep, rest = url.partition("://")
        if sep and "+" in scheme:
            scheme = scheme.split("+", 1)[0]
        return f"{scheme}{sep}{rest}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
