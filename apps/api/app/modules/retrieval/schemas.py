"""Request/response models of the search API and the evidence endpoint (contract m04 §3-§4)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Mode = Literal["hybrid", "keyword", "vector"]
ContentType = Annotated[str, StringConstraints(min_length=1, max_length=127)]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    workspace_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)
    content_types: list[ContentType] | None = Field(default=None, max_length=20)
    mode: Mode = "hybrid"
    limit: int | None = Field(default=None, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class Snippet(BaseModel):
    evidence_id: str | None = None
    chunk_id: uuid.UUID | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    highlights: list[list[int]]


class SearchResult(BaseModel):
    document_id: uuid.UUID
    title: str
    workspace_id: uuid.UUID
    workspace_slug: str
    content_type: str
    version_id: uuid.UUID
    version_number: int
    score: float
    snippets: list[Snippet]


class SearchScopeInfo(BaseModel):
    workspace_count: int


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    degraded: list[str]
    took_ms: int
    scope: SearchScopeInfo


class Neighbors(BaseModel):
    prev_chunk_id: uuid.UUID | None = None
    next_chunk_id: uuid.UUID | None = None


class ChunkEvidence(BaseModel):
    chunk_id: uuid.UUID
    text: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str]
    locators: list[dict[str, Any]]
    version_number: int
    title: str
    neighbors: Neighbors
