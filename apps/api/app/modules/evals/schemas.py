"""Request/response models of the golden-set API (contract m12 section M12.03)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_CASES_PER_SET = 50  # a run is synchronous and bounded by this


class SetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class SetOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    version: int
    case_count: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class SetList(BaseModel):
    items: list[SetOut]


class CaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    expected_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    expected_chunk_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    should_abstain: bool = False
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class CasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str | None = Field(default=None, min_length=1, max_length=1000)
    expected_document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    expected_chunk_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    should_abstain: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CaseOut(BaseModel):
    id: uuid.UUID
    set_id: uuid.UUID
    question: str
    expected_document_ids: list[uuid.UUID]
    expected_chunk_ids: list[uuid.UUID]
    should_abstain: bool
    notes: str | None
    created_at: datetime


class CaseList(BaseModel):
    items: list[CaseOut]


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(default=5, ge=1, le=20)
    # Evidence scoring below this counts as "no answer" (abstained). 0 = any evidence answers.
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    mode: Literal["hybrid", "keyword", "vector"] = "hybrid"


class RetrievedOut(BaseModel):
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    title: str
    score: float


class ResultOut(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID | None
    question: str
    retrieved: list[RetrievedOut]
    answer_status: Literal["answered", "abstained", "error"]
    metrics: dict[str, Any]


class RunOut(BaseModel):
    id: uuid.UUID
    set_id: uuid.UUID
    set_version: int
    state: str
    config: dict[str, Any]
    summary: dict[str, Any]
    created_by: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    results: list[ResultOut] | None = None


class RunList(BaseModel):
    items: list[RunOut]
