"""Request/response models of the research API (M07.02; contract m07 section 4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.ai.chat_schemas import CitationOut

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
State = Literal["planned", "running", "completed", "failed", "cancelled"]


class LimitsIn(BaseModel):
    """Every field can only LOWER the configured ceiling (a higher value is a 422)."""

    model_config = ConfigDict(extra="forbid")

    max_subqueries: int | None = Field(default=None, ge=1)
    max_evidence: int | None = Field(default=None, ge=1)
    max_model_calls: int | None = Field(default=None, ge=2)
    timeout_seconds: int | None = Field(default=None, ge=1)


class ResearchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Question
    workspace_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    limits: LimitsIn | None = None


class ResearchAccepted(BaseModel):
    id: uuid.UUID
    state: State


class PlanOut(BaseModel):
    subqueries: list[str]


class StepOut(BaseModel):
    seq: int
    kind: Literal["plan", "search", "synthesize", "report"]
    query: str | None
    state: Literal["pending", "running", "done", "failed"]


class StatementOut(BaseModel):
    text: str
    citations: list[CitationOut]


class SectionOut(BaseModel):
    heading: str
    statements: list[StatementOut]


class ReportOut(BaseModel):
    sections: list[SectionOut]
    verification: dict[str, Any]
    created_at: datetime


class ResearchOut(BaseModel):
    id: uuid.UUID
    question: str
    state: State
    plan: PlanOut
    steps: list[StepOut]
    usage: dict[str, Any]
    limits: dict[str, Any]
    error_code: str | None = None
    message: str | None = None
    report: ReportOut | None = None
    created_at: datetime
    finished_at: datetime | None = None


class ResearchSummary(BaseModel):
    id: uuid.UUID
    question: str
    state: State
    error_code: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class ResearchList(BaseModel):
    items: list[ResearchSummary]
    next_cursor: str | None


class CancelOut(BaseModel):
    state: State
