"""Settings of the safe URL capture (m06-contracts.md section 5); no ``core.config`` edit."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class UrlCaptureSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    URL_CAPTURE_MAX_BYTES: int = Field(default=10 * 1024 * 1024, ge=1, le=1024**3)
    URL_CAPTURE_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=120)
    URL_CAPTURE_ALLOWED_PORTS: str = "80,443"
    URL_CAPTURE_MAX_REDIRECTS: int = Field(default=3, ge=0, le=10)

    @property
    def allowed_ports(self) -> frozenset[int]:
        return frozenset(int(p) for p in self.URL_CAPTURE_ALLOWED_PORTS.split(",") if p.strip())


@lru_cache(maxsize=1)
def get_url_capture_settings() -> UrlCaptureSettings:
    return UrlCaptureSettings()
