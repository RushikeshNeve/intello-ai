"""Request/response models of the memory API (M07.01; contract m07 section 3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Kind = Literal["fact", "preference", "decision"]
Visibility = Literal["private", "workspace"]
State = Literal["suggested", "approved", "rejected", "expired", "invalidated"]
Statement = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID


class SuggestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: Statement
    kind: Kind
    workspace_id: uuid.UUID | None = None
    evidence: list[EvidenceIn] = Field(default_factory=list, max_length=10)
    expires_at: datetime | None = None
    visibility: Visibility = "private"
    # Additive (optional): where the suggestion came from ("Remember this" on a chat answer).
    source: Literal["user", "chat"] = "user"
    source_ref: uuid.UUID | None = None


class EvidenceOut(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    workspace_id: uuid.UUID | None
    # Only on ``GET /memory/{id}``: ``ok`` or ``stale`` (re-resolved now).
    evidence_status: Literal["ok", "stale"] | None = None
    title: str | None = None


class MemoryOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    owner_id: uuid.UUID
    visibility: Visibility
    kind: Kind
    statement: str
    state: State
    evidence: list[EvidenceOut]
    source: Literal["user", "chat", "research"]
    source_ref: uuid.UUID | None = None
    expires_at: datetime | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    invalidated_reason: str | None
    created_at: datetime
    updated_at: datetime


class MemoryList(BaseModel):
    items: list[MemoryOut]
    next_cursor: str | None
