"""Read-side ports of the graph (M05.02). Names follow contract m05 §2 exactly.

Implementations (the RushDB adapter, the in-memory fake) must already filter by
``org_id``, ``workspace_id IN scope``, ``published`` and ``verification='verified'``;
``traversal`` re-checks every record anyway (defence in depth, a leaky adapter is not trusted).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from app.modules.knowledge.ports import SearchHit
from app.modules.retrieval.scope import RetrievalScope


@dataclass(frozen=True, slots=True)
class GEntity:
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    type: str
    canonical_name: str
    entity_state: str = "active"


@dataclass(frozen=True, slots=True)
class GClaim:
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    subject_id: uuid.UUID
    predicate: str
    object_id: uuid.UUID | None
    object_text: str | None
    confidence: float
    verification: str
    revision_id: uuid.UUID
    published: bool


@dataclass(frozen=True, slots=True)
class GEvidence:
    id: uuid.UUID
    claim_id: uuid.UUID
    chunk_id: uuid.UUID
    revision_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    excerpt: str = ""


@dataclass(frozen=True, slots=True)
class ChunkRef:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID
    workspace_id: uuid.UUID


class GraphReadRepository(Protocol):
    def neighbors(
        self, scope: RetrievalScope, entity_ids: Sequence[uuid.UUID], limit: int
    ) -> list[GClaim]:
        """One hop: claims (subject or object) incident to ``entity_ids``, at most ``limit``."""
        ...

    def get_entities(
        self, scope: RetrievalScope, entity_ids: Sequence[uuid.UUID]
    ) -> list[GEntity]: ...

    def get_claim(self, scope: RetrievalScope, claim_id: uuid.UUID) -> GClaim | None: ...

    def search_entities(
        self,
        scope: RetrievalScope,
        q: str,
        type: str | None,
        limit: int,
        after: tuple[str, uuid.UUID] | None = None,
    ) -> list[GEntity]:
        """Active entities whose name contains ``q`` (case-insensitive; '' = all), ordered by
        ``(canonical_name.casefold(), id)``, strictly after the keyset ``after``."""
        ...

    def get_evidence(
        self, scope: RetrievalScope, claim_ids: Sequence[uuid.UUID]
    ) -> list[GEvidence]: ...


class ChunkSource(Protocol):
    """Original chunk text for graph evidence (never from graph nodes)."""

    def fetch_chunks(
        self, session: Session, scope: RetrievalScope, refs: Sequence[ChunkRef]
    ) -> list[SearchHit]: ...
