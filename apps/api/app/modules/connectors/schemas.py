"""Request/response models of the sources API (contract m06 section 2). Never a secret in a response
except the webhook secret, shown ONCE at creation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class CreateSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["github"]
    repo: str = Field(min_length=3, max_length=201)
    ref: str | None = Field(default=None, max_length=200)
    include: list[str] = Field(default_factory=list, max_length=20)
    token: SecretStr | None = Field(default=None, repr=False, max_length=512)
    sync_interval_seconds: int | None = Field(default=None, ge=300, le=2_592_000)


class SyncSummary(BaseModel):
    id: uuid.UUID
    state: str
    trigger: str
    error_code: str | None
    stats: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ItemCounts(BaseModel):
    active: int
    deleted: int


class SourceOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    provider: str
    name: str
    config: dict[str, Any]
    state: str
    has_token: bool
    sync_interval_seconds: int
    next_sync_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None
    last_sync: SyncSummary | None = None
    items: ItemCounts | None = None


class SourceCreated(SourceOut):
    webhook_secret: str | None = None  # shown once; ``None`` when no encryption key is configured
    webhook_path: str


class SyncAccepted(BaseModel):
    sync_id: uuid.UUID | None  # ``None``: a sync is already queued, nothing new was added
    state: Literal["queued", "already_queued"]
