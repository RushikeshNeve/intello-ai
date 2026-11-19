"""Graph read API schemas (contract m05 §5). The web explorer builds against these shapes."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class NodeOut(BaseModel):
    id: uuid.UUID
    type: str
    name: str
    degree_visible: int


class EdgeOut(BaseModel):
    id: uuid.UUID  # == claim_id
    predicate: str
    subject_id: uuid.UUID
    object_id: uuid.UUID
    claim_id: uuid.UUID
    confidence: float
    evidence_count: int


class NeighborhoodResponse(BaseModel):
    nodes: list[NodeOut]
    edges: list[EdgeOut]
    truncated: bool
    next_cursor: str | None


class EntityOut(BaseModel):
    id: uuid.UUID
    type: str
    name: str


class EntityListResponse(BaseModel):
    items: list[EntityOut]
    next_cursor: str | None


class ClaimEntityOut(BaseModel):
    id: uuid.UUID
    type: str
    name: str


class ClaimOut(BaseModel):
    id: uuid.UUID
    predicate: str
    subject: ClaimEntityOut
    object: ClaimEntityOut | None
    object_text: str | None
    confidence: float


class ClaimEvidenceOut(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    title: str
    page_start: int | None
    page_end: int | None
    excerpt: str


class ClaimDetailResponse(BaseModel):
    claim: ClaimOut
    evidence: list[ClaimEvidenceOut]
