"""Retrieval settings: its OWN settings class over the ``SEARCH_*`` env names (contract §11)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    SEARCH_DEFAULT_LIMIT: int = Field(default=10, ge=1, le=50)
    SEARCH_CANDIDATES: int = Field(default=50, ge=1, le=200)
    SEARCH_MAX_PER_DOCUMENT: int = Field(default=3, ge=1, le=10)
    SEARCH_RATE_PER_MIN: int = Field(default=60, ge=1, le=100_000)


@lru_cache(maxsize=1)
def get_search_settings() -> SearchSettings:
    return SearchSettings()
