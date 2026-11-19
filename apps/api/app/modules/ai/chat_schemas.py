"""Request/response models of the chat API (M04.02.04; contract m04 §14 "Chat API (as built)")."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TitleStr = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class ThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID | None = None
    title: TitleStr = ""


class ThreadRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: TitleStr


class ThreadOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    title: str
    created_at: datetime
    updated_at: datetime


class ThreadList(BaseModel):
    items: list[ThreadOut]
    next_cursor: str | None


class CitationOut(BaseModel):
    marker: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    version_number: int
    page_start: int | None
    page_end: int | None
    excerpt: str


class VerificationOut(BaseModel):
    status: Literal["grounded", "partial", "abstained"]
    withheld_count: int
    flagged_count: int


class MessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    citations: list[CitationOut]
    verification: VerificationOut | None
    run_id: uuid.UUID | None
    created_at: datetime


class ActiveRun(BaseModel):
    run_id: uuid.UUID
    state: str


class ThreadDetail(BaseModel):
    thread: ThreadOut
    messages: list[MessageOut]
    active_run: ActiveRun | None


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    idempotency_key: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    workspace_ids: Annotated[list[uuid.UUID], Field(max_length=1000)] | None = None


class RunAccepted(BaseModel):
    run_id: uuid.UUID
    message_id: uuid.UUID
    state: str


class RunOut(BaseModel):
    run_id: uuid.UUID
    thread_id: uuid.UUID
    state: str
    error_code: str | None
    message: MessageOut | None
    created_at: datetime
    finished_at: datetime | None


class CancelOut(BaseModel):
    state: str
