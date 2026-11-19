"""In-memory graph read repository for tests and the benchmark (seeded from plain records)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy.orm import Session

from app.modules.graph.ports_read import ChunkRef, GClaim, GEntity, GEvidence
from app.modules.knowledge.ports import SearchHit
from app.modules.retrieval.scope import RetrievalScope


class FakeGraphRead:
    """``leaky=True`` returns everything regardless of scope/flags (to test the defence in depth
    of ``traverse``). ``calls`` records method names (a spy for the router tests)."""

    def __init__(
        self,
        entities: Iterable[GEntity] = (),
        claims: Iterable[GClaim] = (),
        evidence: Iterable[GEvidence] = (),
        *,
        leaky: bool = False,
    ) -> None:
        self.entities = {e.id: e for e in entities}
        self.claims = {c.id: c for c in claims}
        self.evidence = list(evidence)
        self.leaky = leaky
        self.calls: list[str] = []

    def _ok(self, scope: RetrievalScope, org_id: uuid.UUID, ws: uuid.UUID) -> bool:
        return self.leaky or (org_id == scope.org_id and ws in scope.workspace_ids)

    def _claim_ok(self, scope: RetrievalScope, c: GClaim) -> bool:
        return self.leaky or (
            self._ok(scope, c.org_id, c.workspace_id)
            and c.published
            and c.verification == "verified"
        )

    def neighbors(
        self, scope: RetrievalScope, entity_ids: Sequence[uuid.UUID], limit: int
    ) -> list[GClaim]:
        self.calls.append("neighbors")
        ids = set(entity_ids)
        found = [
            c
            for c in self.claims.values()
            if (c.subject_id in ids or c.object_id in ids) and self._claim_ok(scope, c)
        ]
        found.sort(key=lambda c: (-c.confidence, str(c.id)))
        return found[:limit]

    def get_entities(self, scope: RetrievalScope, entity_ids: Sequence[uuid.UUID]) -> list[GEntity]:
        self.calls.append("get_entities")
        return [
            e
            for i in entity_ids
            if (e := self.entities.get(i)) is not None and self._ok(scope, e.org_id, e.workspace_id)
        ]

    def get_claim(self, scope: RetrievalScope, claim_id: uuid.UUID) -> GClaim | None:
        self.calls.append("get_claim")
        c = self.claims.get(claim_id)
        return c if c is not None and self._claim_ok(scope, c) else None

    def search_entities(
        self,
        scope: RetrievalScope,
        q: str,
        type: str | None,
        limit: int,
        after: tuple[str, uuid.UUID] | None = None,
    ) -> list[GEntity]:
        self.calls.append("search_entities")
        needle = q.casefold()
        rows = [
            e
            for e in self.entities.values()
            if self._ok(scope, e.org_id, e.workspace_id)
            and e.entity_state == "active"
            and needle in e.canonical_name.casefold()
            and (type is None or e.type == type)
        ]
        rows.sort(key=lambda e: (e.canonical_name.casefold(), str(e.id)))
        if after is not None:
            key = (after[0], str(after[1]))
            rows = [e for e in rows if (e.canonical_name.casefold(), str(e.id)) > key]
        return rows[:limit]

    def get_evidence(
        self, scope: RetrievalScope, claim_ids: Sequence[uuid.UUID]
    ) -> list[GEvidence]:
        self.calls.append("get_evidence")
        wanted = set(claim_ids)
        return [
            ev
            for ev in self.evidence
            if ev.claim_id in wanted
            and (c := self.claims.get(ev.claim_id)) is not None
            and self._claim_ok(scope, c)
        ]


class FakeChunkSource:
    """``chunks``: chunk_id -> text. Unknown chunk ids are skipped (deleted artifacts)."""

    def __init__(self, chunks: Mapping[uuid.UUID, str]) -> None:
        self.chunks = dict(chunks)
        self.calls = 0

    def fetch_chunks(
        self, session: Session, scope: RetrievalScope, refs: Sequence[ChunkRef]
    ) -> list[SearchHit]:
        del session, scope
        self.calls += 1
        out: list[SearchHit] = []
        for r in refs:
            body = self.chunks.get(r.chunk_id)
            if body is None:
                continue
            out.append(
                SearchHit(
                    chunk_id=r.chunk_id,
                    document_id=r.document_id,
                    version_id=r.version_id,
                    revision_id=r.revision_id,
                    workspace_id=r.workspace_id,
                    score=0.0,
                    text=body,
                    locators=[],
                    heading_path=[],
                    page_start=1,
                    page_end=1,
                )
            )
        return out
