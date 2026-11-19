"""In-memory ``KnowledgeRepository`` for fast tests and other modules' tests (M03.03.01).

Same semantics as the RushDB adapter, enforced by the shared contract suite
(``tests/infrastructure_rushdb/test_contract.py``): tenant isolation by organization, workspace and
``published`` filtering in one private place, idempotent upsert (a re-write never un-publishes),
cosine vector search, BM25 keyword scoring (k1=1.2, b=0.75, whole-tenant corpus statistics, the
same tokenizer as RushDB: lower-cased alphanumeric runs) and reciprocal-rank fusion with k=60.
Indexes must be ensured first, as with the real store. For tests only.
"""

from __future__ import annotations

import math
import re
import threading
import uuid
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Final

from app.modules.knowledge.ports import (
    ChunkRecord,
    ClaimRecord,
    DeleteCounts,
    EntityRecord,
    EvidenceRecord,
    GraphChunk,
    KnowledgeStoreError,
    OrgScope,
    SearchFilters,
    SearchHit,
    validate_limit,
    validate_query_text,
    validate_space_id,
    validate_vector,
)

_TOKEN = re.compile(r"[a-z0-9]+")
_K1: Final = 1.2
_B: Final = 0.75
_RRF_K: Final = 60


@dataclass
class _Stored:
    chunk: ChunkRecord
    vector: list[float]
    tokens: list[str]
    published: bool = False


@dataclass
class _Tenant:
    chunks: dict[uuid.UUID, _Stored]
    dimensions: dict[str, int]
    text_index: bool = False
    claims: dict[uuid.UUID, ClaimRecord] = field(default_factory=dict)
    evidence: dict[uuid.UUID, EvidenceRecord] = field(default_factory=dict)
    entities: dict[uuid.UUID, EntityRecord] = field(default_factory=dict)


def _tokens(value: str) -> list[str]:
    return _TOKEN.findall(value.lower())


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


class InMemoryKnowledgeRepository:
    """Process-local fake. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tenants: dict[uuid.UUID, _Tenant] = {}

    def __repr__(self) -> str:
        return "InMemoryKnowledgeRepository()"

    def _tenant(self, scope: OrgScope) -> _Tenant:
        return self._tenants.setdefault(scope.org_id, _Tenant(chunks={}, dimensions={}))

    # ------------------------------------------------------------------------- indexes

    def ensure_indexes(self, scope: OrgScope, embedding_space_id: str, dimensions: int) -> None:
        space = validate_space_id(embedding_space_id)
        if isinstance(dimensions, bool) or not 1 <= dimensions <= 16384:
            raise ValueError("dimensions must be between 1 and 16384")
        with self._lock:
            tenant = self._tenant(scope)
            existing = tenant.dimensions.get(space)
            if existing is not None and existing != dimensions:
                raise KnowledgeStoreError(
                    "memory ensure_indexes: space exists with other dimensions", code="conflict"
                )
            tenant.dimensions[space] = dimensions
            tenant.text_index = True

    def _dims(self, tenant: _Tenant, space: str) -> int:
        dims = tenant.dimensions.get(space)
        if dims is None:
            raise KnowledgeStoreError(
                "memory: vector index for the embedding space is missing; call ensure_indexes",
                code="index_missing",
            )
        return dims

    # -------------------------------------------------------------------------- writes

    def upsert_chunks(
        self,
        scope: OrgScope,
        chunks: Sequence[ChunkRecord],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        if not chunks:
            return 0
        space = chunks[0].embedding_space_id
        for chunk in chunks:
            if chunk.org_id != scope.org_id:
                raise ValueError("chunk belongs to a different organization than the scope")
            if chunk.embedding_space_id != space:
                raise ValueError("all chunks of one call must share one embedding space")
        with self._lock:
            tenant = self._tenant(scope)
            dims = self._dims(tenant, space)
            for vector in vectors:
                validate_vector(vector, dims)
            for chunk, vector in zip(chunks, vectors, strict=True):
                previous = tenant.chunks.get(chunk.chunk_id)
                tenant.chunks[chunk.chunk_id] = _Stored(
                    chunk=deepcopy(chunk),
                    vector=[float(v) for v in vector],
                    tokens=_tokens(chunk.text),
                    published=previous.published if previous else False,
                )
        return len(chunks)

    def set_published(self, scope: OrgScope, revision_id: uuid.UUID, flag: bool) -> int:
        if not isinstance(flag, bool):
            raise TypeError("flag must be a bool")
        changed = 0
        with self._lock:
            tenant = self._tenant(scope)
            for stored in tenant.chunks.values():
                if stored.chunk.revision_id == revision_id and stored.published != flag:
                    stored.published = flag
                    changed += 1
            for cid, claim in list(tenant.claims.items()):
                if claim.revision_id == revision_id and claim.published != flag:
                    tenant.claims[cid] = replace(claim, published=flag)
        return changed

    def _purge(
        self, scope: OrgScope, *, revision_id: uuid.UUID | None, document_id: uuid.UUID | None
    ) -> DeleteCounts:
        with self._lock:
            tenant = self._tenant(scope)
            chunks = tenant.chunks
            self._drop_graph(tenant, revision_id=revision_id, document_id=document_id)
            doomed = [
                cid
                for cid, s in chunks.items()
                if (revision_id is not None and s.chunk.revision_id == revision_id)
                or (document_id is not None and s.chunk.document_id == document_id)
            ]
            for cid in doomed:
                del chunks[cid]
        return DeleteCounts(chunks=len(doomed), evidence=0)

    @staticmethod
    def _drop_graph(
        tenant: _Tenant, *, revision_id: uuid.UUID | None, document_id: uuid.UUID | None
    ) -> int:
        doomed = [
            cid
            for cid, c in tenant.claims.items()
            if (revision_id is not None and c.revision_id == revision_id)
            or (document_id is not None and c.document_id == document_id)
        ]
        for cid in doomed:
            del tenant.claims[cid]
        for eid, ev in list(tenant.evidence.items()):
            if ev.claim_id in doomed or (revision_id is not None and ev.revision_id == revision_id):
                del tenant.evidence[eid]
        return len(doomed)

    def delete_revision(self, scope: OrgScope, revision_id: uuid.UUID) -> DeleteCounts:
        return self._purge(scope, revision_id=revision_id, document_id=None)

    def delete_document(self, scope: OrgScope, document_id: uuid.UUID) -> DeleteCounts:
        return self._purge(scope, revision_id=None, document_id=document_id)

    # --------------------------------------------------------------------------- reads

    def count_chunks(
        self, scope: OrgScope, revision_id: uuid.UUID, published: bool | None = None
    ) -> int:
        with self._lock:
            return sum(
                1
                for s in self._tenant(scope).chunks.values()
                if s.chunk.revision_id == revision_id
                and (published is None or s.published == published)
            )

    def list_chunk_ids(self, scope: OrgScope, revision_id: uuid.UUID) -> list[uuid.UUID]:
        with self._lock:
            found = [
                s.chunk
                for s in self._tenant(scope).chunks.values()
                if s.chunk.revision_id == revision_id
            ]
        return [c.chunk_id for c in sorted(found, key=lambda c: c.chunk_index)]

    # ------------------------------------------------------------------------- searches

    def _candidates(
        self, tenant: _Tenant, filters: SearchFilters, space: str | None
    ) -> list[_Stored]:
        """THE filter: authorized workspaces AND published (and the space, for vector legs)."""
        if not isinstance(filters, SearchFilters):
            raise TypeError("filters must be SearchFilters")
        return [
            s
            for s in tenant.chunks.values()
            if s.chunk.workspace_id in filters.workspace_ids
            and s.published
            and (space is None or s.chunk.embedding_space_id == space)
        ]

    def _bm25(
        self, tenant: _Tenant, candidates: list[_Stored], query: str
    ) -> list[tuple[_Stored, float]]:
        if not tenant.text_index:
            raise KnowledgeStoreError(
                "memory: text index is missing; call ensure_indexes", code="index_missing"
            )
        terms = sorted(set(_tokens(query)))
        corpus = list(tenant.chunks.values())  # whole-tenant statistics, like RushDB
        n = len(corpus)
        if n == 0 or not terms:
            return []
        avg = sum(len(s.tokens) for s in corpus) / n
        df = {t: sum(1 for s in corpus if t in set(s.tokens)) for t in terms}
        scored: list[tuple[_Stored, float]] = []
        for stored in candidates:
            counts = Counter(stored.tokens)
            score = 0.0
            for t in terms:
                tf = counts.get(t, 0)
                if tf == 0:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += (
                    idf * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * len(stored.tokens) / avg))
                )
            if score > 0:
                scored.append((stored, score))
        scored.sort(key=lambda p: (-p[1], str(p[0].chunk.chunk_id)))
        return scored

    @staticmethod
    def _hit(stored: _Stored, score: float) -> SearchHit:
        c = stored.chunk
        return SearchHit(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            version_id=c.version_id,
            revision_id=c.revision_id,
            workspace_id=c.workspace_id,
            score=score,
            text=c.text,
            locators=[dict(x) for x in deepcopy(list(c.locators))],
            heading_path=list(c.heading_path),
            page_start=c.page_start,
            page_end=c.page_end,
        )

    def search_keyword(
        self, scope: OrgScope, query: str, filters: SearchFilters, *, limit: int = 10
    ) -> list[SearchHit]:
        validate_limit(limit)
        text = validate_query_text(query)
        with self._lock:
            tenant = self._tenant(scope)
            ranked = self._bm25(tenant, self._candidates(tenant, filters, None), text)
            return [self._hit(s, score) for s, score in ranked[:limit]]

    def _vector_rank(
        self, candidates: list[_Stored], vector: Sequence[float]
    ) -> list[tuple[_Stored, float]]:
        scored = [(s, _cosine(s.vector, vector)) for s in candidates]
        scored.sort(key=lambda p: (-p[1], str(p[0].chunk.chunk_id)))
        return scored

    def search_vector(
        self,
        scope: OrgScope,
        embedding_space_id: str,
        vector: Sequence[float],
        filters: SearchFilters,
        *,
        limit: int = 10,
    ) -> list[SearchHit]:
        validate_limit(limit)
        space = validate_space_id(embedding_space_id)
        with self._lock:
            tenant = self._tenant(scope)
            validate_vector(vector, self._dims(tenant, space))
            ranked = self._vector_rank(self._candidates(tenant, filters, space), vector)
            return [self._hit(s, score) for s, score in ranked[:limit]]

    def search_hybrid(
        self,
        scope: OrgScope,
        embedding_space_id: str,
        query: str,
        vector: Sequence[float],
        filters: SearchFilters,
        *,
        limit: int = 10,
    ) -> list[SearchHit]:
        validate_limit(limit)
        text = validate_query_text(query)
        space = validate_space_id(embedding_space_id)
        with self._lock:
            tenant = self._tenant(scope)
            validate_vector(vector, self._dims(tenant, space))
            candidates = self._candidates(tenant, filters, space)
            prefetch = 4 * limit
            fused: dict[uuid.UUID, float] = {}
            by_id: dict[uuid.UUID, _Stored] = {}
            legs = (
                self._vector_rank(candidates, vector)[:prefetch],
                self._bm25(tenant, candidates, text)[:prefetch],
            )
            for leg in legs:
                for rank, (stored, _score) in enumerate(leg):
                    cid = stored.chunk.chunk_id
                    by_id[cid] = stored
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
            order = sorted(fused, key=lambda cid: (-fused[cid], str(cid)))
            return [self._hit(by_id[cid], fused[cid]) for cid in order[:limit]]


class InMemoryGraphRepository(InMemoryKnowledgeRepository):
    """In-memory ``GraphRepository`` (also a ``KnowledgeRepository``: purge and publish flags
    cover graph records exactly like the RushDB adapter). For tests only."""

    def __repr__(self) -> str:
        return "InMemoryGraphRepository()"

    def read_chunks(
        self, scope: OrgScope, revision_id: uuid.UUID, *, limit: int
    ) -> list[GraphChunk]:
        with self._lock:
            found = [
                s.chunk
                for s in self._tenant(scope).chunks.values()
                if s.chunk.revision_id == revision_id
            ]
        found.sort(key=lambda c: c.chunk_index)
        return [
            GraphChunk(
                c.chunk_id,
                c.chunk_index,
                c.text,
                c.workspace_id,
                c.document_id,
                c.version_id,
                c.revision_id,
            )
            for c in found[:limit]
        ]

    def upsert_entities(self, scope: OrgScope, entities: Sequence[EntityRecord]) -> int:
        for e in entities:
            if e.org_id != scope.org_id:
                raise ValueError("entity belongs to a different organization than the scope")
        with self._lock:
            tenant = self._tenant(scope)
            for e in entities:
                tenant.entities[e.id] = e
        return len(entities)

    def upsert_claims(
        self,
        scope: OrgScope,
        claims: Sequence[ClaimRecord],
        evidence: Sequence[EvidenceRecord],
        *,
        published: bool,
    ) -> int:
        for c in claims:
            if c.org_id != scope.org_id:
                raise ValueError("claim belongs to a different organization than the scope")
        with self._lock:
            tenant = self._tenant(scope)
            for c in claims:
                tenant.claims[c.id] = replace(c, published=published)
            for ev in evidence:
                tenant.evidence[ev.id] = ev
        return len(claims)

    def list_claims(
        self, scope: OrgScope, revision_id: uuid.UUID, *, verification: str | None = None
    ) -> list[ClaimRecord]:
        with self._lock:
            found = [
                c
                for c in self._tenant(scope).claims.values()
                if c.revision_id == revision_id
                and (verification is None or c.verification == verification)
            ]
        return sorted(found, key=lambda c: str(c.id))

    def list_evidence(self, scope: OrgScope, claim_id: uuid.UUID) -> list[EvidenceRecord]:
        with self._lock:
            found = [e for e in self._tenant(scope).evidence.values() if e.claim_id == claim_id]
        return sorted(found, key=lambda e: str(e.id))

    def list_entities(self, scope: OrgScope, workspace_id: uuid.UUID) -> list[EntityRecord]:
        with self._lock:
            found = [
                e for e in self._tenant(scope).entities.values() if e.workspace_id == workspace_id
            ]
        return sorted(found, key=lambda e: str(e.id))

    def delete_graph_revision(self, scope: OrgScope, revision_id: uuid.UUID) -> int:
        with self._lock:
            return self._drop_graph(self._tenant(scope), revision_id=revision_id, document_id=None)

    def remap_entity(self, scope: OrgScope, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
        changed = 0
        with self._lock:
            tenant = self._tenant(scope)
            old = tenant.entities.get(from_id)
            if old is not None:
                tenant.entities[from_id] = replace(old, entity_state="merged")
            for cid, c in list(tenant.claims.items()):
                subject = to_id if c.subject_id == from_id else c.subject_id
                obj = to_id if c.object_id == from_id else c.object_id
                if (subject, obj) != (c.subject_id, c.object_id):
                    tenant.claims[cid] = replace(c, subject_id=subject, object_id=obj)
                    changed += 1
        return changed
