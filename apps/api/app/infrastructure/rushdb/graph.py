"""RushDB graph repository (M05.01): the ``GraphRepository`` port on the same HTTP client and tenant
rules as :class:`RushDbKnowledgeRepository` (it extends it, so ``delete_revision``/``set_published``
and the graph methods share one transport; callers may pass the same instance for both ports).

Records (contract m05 section 2): ``Entity``, ``Claim``, ``Evidence``. Relationships are properties
(``subject_id``, ``object_id``, ``claim_id``) so every read is a plain predicate query; every write
is a merge by deterministic id (idempotent, retries never duplicate).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

from app.infrastructure.rushdb.repository import (
    _PUT_RETRY_CODES,
    CLAIM_LABEL,
    EVIDENCE_LABEL,
    RushDbKnowledgeRepository,
    _Json,
)
from app.modules.knowledge.ports import (
    ClaimRecord,
    EntityRecord,
    EvidenceRecord,
    GraphChunk,
    KnowledgeStoreError,
    OrgScope,
)

ENTITY_LABEL: Final = "Entity"
_CHUNK_LABEL: Final = "Chunk"
_PAGE: Final = 200
_MAX_REMAP: Final = 5000

_CHUNK_SELECT = {
    k: k
    for k in (
        "chunk_id",
        "chunk_index",
        "text",
        "workspace_id",
        "document_id",
        "version_id",
        "revision_id",
    )
}
_CLAIM_FIELDS: Final = (
    "claim_id",
    "org_id",
    "workspace_id",
    "document_id",
    "revision_id",
    "subject_id",
    "predicate",
    "object_id",
    "object_text",
    "confidence",
    "verification",
    "extractor_version",
    "published",
)
_EVIDENCE_FIELDS: Final = (
    "evidence_id",
    "claim_id",
    "org_id",
    "workspace_id",
    "chunk_id",
    "revision_id",
    "document_id",
    "version_id",
    "excerpt",
)
_ENTITY_FIELDS: Final = (
    "entity_id",
    "org_id",
    "workspace_id",
    "type",
    "canonical_name",
    "aliases",
    "entity_state",
    "extractor_version",
    "source_revision_id",
)


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _opt_uuid(value: Any) -> uuid.UUID | None:
    return None if value in (None, "") else uuid.UUID(str(value))


class RushDbGraphRepository(RushDbKnowledgeRepository):
    def __repr__(self) -> str:
        return "RushDbGraphRepository()"

    # ------------------------------------------------------------------ helpers

    def _rows(
        self,
        scope: OrgScope,
        label: str,
        predicate: _Json,
        fields: Sequence[str],
        *,
        what: str,
        order: _Json | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """All rows of a predicate query, paged (``limit`` caps the total)."""
        out: list[dict[str, Any]] = []
        offset = 0
        while limit is None or len(out) < limit:
            page_size = _PAGE if limit is None else min(_PAGE, limit - len(out))
            query: _Json = {
                "labels": [label],
                "predicate": predicate,
                "select": {f: f for f in fields},
                "order": order or {fields[0]: "asc"},
                "offset": offset,
                "limit": page_size,
            }
            page = self._execute(
                scope, {"kind": "query", "query": {"kind": "records", "query": query}}, what=what
            )
            rows = [r for r in page.get("rows", []) if isinstance(r, dict)]
            out.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
        return out

    def _put(
        self, scope: OrgScope, label: str, record_id: uuid.UUID, props: _Json, what: str
    ) -> None:
        self._execute(
            scope,
            {
                "kind": "mutate",
                "mutation": {
                    "kind": "putRecord",
                    "mode": "merge",
                    "label": label,
                    "id": str(record_id),
                    "properties": props,
                    "options": {},
                },
            },
            what=what,
            retry_codes=_PUT_RETRY_CODES,
        )

    # ------------------------------------------------------------------- reads

    def read_chunks(
        self, scope: OrgScope, revision_id: uuid.UUID, *, limit: int
    ) -> list[GraphChunk]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        rows = self._rows(
            scope,
            _CHUNK_LABEL,
            {"revision_id": str(revision_id)},
            list(_CHUNK_SELECT),
            what="graph.read_chunks",
            order={"chunk_index": "asc"},
            limit=limit,
        )
        try:
            return [
                GraphChunk(
                    chunk_id=_uuid(r["chunk_id"]),
                    chunk_index=int(r["chunk_index"]),
                    text=str(r.get("text", "")),
                    workspace_id=_uuid(r["workspace_id"]),
                    document_id=_uuid(r["document_id"]),
                    version_id=_uuid(r["version_id"]),
                    revision_id=_uuid(r["revision_id"]),
                )
                for r in rows
            ]
        except (KeyError, TypeError, ValueError):
            raise KnowledgeStoreError(
                "rushdb graph.read_chunks: malformed record", code="malformed"
            ) from None

    def list_claims(
        self, scope: OrgScope, revision_id: uuid.UUID, *, verification: str | None = None
    ) -> list[ClaimRecord]:
        predicate: _Json = {"revision_id": str(revision_id)}
        if verification is not None:
            predicate["verification"] = verification
        rows = self._rows(scope, CLAIM_LABEL, predicate, _CLAIM_FIELDS, what="graph.list_claims")
        return [
            ClaimRecord(
                id=_uuid(r["claim_id"]),
                org_id=_uuid(r["org_id"]),
                workspace_id=_uuid(r["workspace_id"]),
                document_id=_uuid(r["document_id"]),
                revision_id=_uuid(r["revision_id"]),
                subject_id=_uuid(r["subject_id"]),
                predicate=str(r["predicate"]),
                object_id=_opt_uuid(r.get("object_id")),
                object_text=r.get("object_text") or None,
                confidence=float(r.get("confidence", 0.0)),
                verification=str(r["verification"]),
                extractor_version=str(r.get("extractor_version", "")),
                published=bool(r.get("published", False)),
            )
            for r in rows
        ]

    def list_evidence(self, scope: OrgScope, claim_id: uuid.UUID) -> list[EvidenceRecord]:
        rows = self._rows(
            scope,
            EVIDENCE_LABEL,
            {"claim_id": str(claim_id)},
            _EVIDENCE_FIELDS,
            what="graph.list_evidence",
        )
        return [
            EvidenceRecord(
                id=_uuid(r["evidence_id"]),
                claim_id=_uuid(r["claim_id"]),
                org_id=_uuid(r["org_id"]),
                workspace_id=_uuid(r["workspace_id"]),
                chunk_id=_uuid(r["chunk_id"]),
                revision_id=_uuid(r["revision_id"]),
                document_id=_uuid(r["document_id"]),
                version_id=_uuid(r["version_id"]),
                excerpt=str(r.get("excerpt", "")),
            )
            for r in rows
        ]

    def list_entities(self, scope: OrgScope, workspace_id: uuid.UUID) -> list[EntityRecord]:
        rows = self._rows(
            scope,
            ENTITY_LABEL,
            {"workspace_id": str(workspace_id)},
            _ENTITY_FIELDS,
            what="graph.list_entities",
        )
        out: list[EntityRecord] = []
        for r in rows:
            try:
                aliases = json.loads(r.get("aliases") or "[]")
            except ValueError:
                aliases = []
            out.append(
                EntityRecord(
                    id=_uuid(r["entity_id"]),
                    org_id=_uuid(r["org_id"]),
                    workspace_id=_uuid(r["workspace_id"]),
                    type=str(r["type"]),
                    canonical_name=str(r["canonical_name"]),
                    aliases=[str(a) for a in aliases if isinstance(a, str)],
                    entity_state=str(r["entity_state"]),
                    extractor_version=str(r.get("extractor_version", "")),
                    source_revision_id=_uuid(r["source_revision_id"]),
                )
            )
        return out

    # ------------------------------------------------------------------ writes

    def upsert_entities(self, scope: OrgScope, entities: Sequence[EntityRecord]) -> int:
        now = datetime.now(UTC).isoformat()
        for e in entities:
            if e.org_id != scope.org_id:
                raise ValueError("entity belongs to a different organization than the scope")
        for e in entities:
            self._put(
                scope,
                ENTITY_LABEL,
                e.id,
                {
                    "entity_id": str(e.id),
                    "org_id": str(e.org_id),
                    "workspace_id": str(e.workspace_id),
                    "type": e.type,
                    "canonical_name": e.canonical_name,
                    "aliases": json.dumps(list(e.aliases), separators=(",", ":")),
                    "entity_state": e.entity_state,
                    "extractor_version": e.extractor_version,
                    "source_revision_id": str(e.source_revision_id),
                    "created_at": now,
                },
                "graph.upsert_entity",
            )
        return len(entities)

    def upsert_claims(
        self,
        scope: OrgScope,
        claims: Sequence[ClaimRecord],
        evidence: Sequence[EvidenceRecord],
        *,
        published: bool,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        for c in claims:
            if c.org_id != scope.org_id:
                raise ValueError("claim belongs to a different organization than the scope")
        for c in claims:
            props: _Json = {
                "claim_id": str(c.id),
                "org_id": str(c.org_id),
                "workspace_id": str(c.workspace_id),
                "document_id": str(c.document_id),
                "revision_id": str(c.revision_id),
                "source_revision_id": str(c.revision_id),
                "subject_id": str(c.subject_id),
                "predicate": c.predicate,
                "confidence": float(c.confidence),
                "verification": c.verification,
                "extractor_version": c.extractor_version,
                "published": published,
                "created_at": now,
            }
            if c.object_id is not None:
                props["object_id"] = str(c.object_id)
            if c.object_text is not None:
                props["object_text"] = c.object_text
            self._put(scope, CLAIM_LABEL, c.id, props, "graph.upsert_claim")
        for ev in evidence:
            self._put(
                scope,
                EVIDENCE_LABEL,
                ev.id,
                {
                    "evidence_id": str(ev.id),
                    "claim_id": str(ev.claim_id),
                    "org_id": str(ev.org_id),
                    "workspace_id": str(ev.workspace_id),
                    "chunk_id": str(ev.chunk_id),
                    "revision_id": str(ev.revision_id),
                    "document_id": str(ev.document_id),
                    "version_id": str(ev.version_id),
                    "excerpt": ev.excerpt,
                    "created_at": now,
                },
                "graph.upsert_evidence",
            )
        return len(claims)

    def delete_graph_revision(self, scope: OrgScope, revision_id: uuid.UUID) -> int:
        removed = 0
        for label in (EVIDENCE_LABEL, CLAIM_LABEL):
            result = self._execute(
                scope,
                {
                    "kind": "mutate",
                    "mutation": {
                        "kind": "deleteRecords",
                        "query": {
                            "labels": [label],
                            "predicate": {"revision_id": str(revision_id)},
                        },
                    },
                },
                what=f"graph.delete.{label.lower()}",
            )
            affected = result.get("affected", 0)
            if label == CLAIM_LABEL and isinstance(affected, int):
                removed = affected
        return removed

    def remap_entity(self, scope: OrgScope, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
        existing = self._rows(
            scope,
            ENTITY_LABEL,
            {"entity_id": str(from_id)},
            ["entity_id"],
            what="graph.remap.lookup",
            limit=1,
        )
        if existing:
            self._put(
                scope,
                ENTITY_LABEL,
                from_id,
                {"entity_state": "merged", "merged_into": str(to_id)},
                "graph.remap.entity",
            )
        changed = 0
        for prop in ("subject_id", "object_id"):
            rows = self._rows(
                scope,
                CLAIM_LABEL,
                {prop: str(from_id)},
                ["claim_id"],
                what="graph.remap.list",
                limit=_MAX_REMAP,
            )
            for r in rows:
                self._put(
                    scope,
                    CLAIM_LABEL,
                    _uuid(r["claim_id"]),
                    {prop: str(to_id)},
                    "graph.remap.claim",
                )
                changed += 1
        return changed
