"""``GraphReadRepository`` over the graph store (RushDB or in-memory ``GraphStore``).

The store applies the single visibility rule (org tenant, ``workspace_id IN scope``, published,
verified); this adapter only maps records to the read DTOs of ``ports_read``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from app.modules.graph.ports_read import GClaim, GEntity, GEvidence
from app.modules.knowledge.ports import ClaimRecord, EntityRecord, EvidenceRecord, OrgScope
from app.modules.retrieval.scope import RetrievalScope

_WS = frozenset[uuid.UUID]


class GraphStore(Protocol):
    """The scoped read methods of ``RushDbGraphRepository`` / ``InMemoryGraphRepository``."""

    def read_claims_incident(
        self, scope: OrgScope, workspace_ids: _WS, entity_ids: Sequence[uuid.UUID], limit: int
    ) -> list[ClaimRecord]: ...

    def read_claim(
        self, scope: OrgScope, workspace_ids: _WS, claim_id: uuid.UUID
    ) -> ClaimRecord | None: ...

    def read_entities(
        self, scope: OrgScope, workspace_ids: _WS, entity_ids: Sequence[uuid.UUID]
    ) -> list[EntityRecord]: ...

    def search_entities(
        self,
        scope: OrgScope,
        workspace_ids: _WS,
        q: str,
        type: str | None,
        limit: int,
        after: tuple[str, uuid.UUID] | None,
    ) -> list[EntityRecord]: ...

    def read_evidence(
        self, scope: OrgScope, workspace_ids: _WS, claim_ids: Sequence[uuid.UUID]
    ) -> list[EvidenceRecord]: ...


def _claim(c: ClaimRecord) -> GClaim:
    return GClaim(
        c.id,
        c.org_id,
        c.workspace_id,
        c.subject_id,
        c.predicate,
        c.object_id,
        c.object_text,
        c.confidence,
        c.verification,
        c.revision_id,
        c.published,
    )


def _entity(e: EntityRecord) -> GEntity:
    return GEntity(e.id, e.org_id, e.workspace_id, e.type, e.canonical_name, e.entity_state)


def _evidence(e: EvidenceRecord) -> GEvidence:
    return GEvidence(
        e.id, e.claim_id, e.chunk_id, e.revision_id, e.document_id, e.version_id, e.excerpt
    )


class StoreGraphReadRepository:
    def __init__(self, store: GraphStore) -> None:
        self._store = store

    def neighbors(
        self, scope: RetrievalScope, entity_ids: Sequence[uuid.UUID], limit: int
    ) -> list[GClaim]:
        rows = self._store.read_claims_incident(scope.org, scope.workspace_ids, entity_ids, limit)
        return [_claim(c) for c in rows]

    def get_entities(self, scope: RetrievalScope, entity_ids: Sequence[uuid.UUID]) -> list[GEntity]:
        rows = self._store.read_entities(scope.org, scope.workspace_ids, entity_ids)
        return [_entity(e) for e in rows]

    def get_claim(self, scope: RetrievalScope, claim_id: uuid.UUID) -> GClaim | None:
        c = self._store.read_claim(scope.org, scope.workspace_ids, claim_id)
        return _claim(c) if c is not None else None

    def search_entities(
        self,
        scope: RetrievalScope,
        q: str,
        type: str | None,
        limit: int,
        after: tuple[str, uuid.UUID] | None = None,
    ) -> list[GEntity]:
        rows = self._store.search_entities(scope.org, scope.workspace_ids, q, type, limit, after)
        return [_entity(e) for e in rows]

    def get_evidence(
        self, scope: RetrievalScope, claim_ids: Sequence[uuid.UUID]
    ) -> list[GEvidence]:
        rows = self._store.read_evidence(scope.org, scope.workspace_ids, claim_ids)
        return [_evidence(e) for e in rows]
