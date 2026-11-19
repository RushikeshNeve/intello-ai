"""Builders for the graph retrieval unit tests (fake graph, scopes, publication rows)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from app.modules.graph.fake_read import FakeChunkSource, FakeGraphRead
from app.modules.graph.ports_read import GClaim, GEntity, GEvidence
from tests.retrieval.support import FakeDb, make_scope

ORG = uuid.uuid4()
WS1 = uuid.uuid4()
WS2 = uuid.uuid4()


def ent(name: str, ws: uuid.UUID = WS1, *, org: uuid.UUID = ORG, state: str = "active") -> GEntity:
    return GEntity(uuid.uuid4(), org, ws, "concept", name, state)


def claim(
    s: GEntity,
    o: GEntity | None,
    *,
    predicate: str = "depends_on",
    ws: uuid.UUID | None = None,
    verified: bool = True,
    published: bool = True,
    confidence: float = 0.9,
) -> GClaim:
    return GClaim(
        id=uuid.uuid4(),
        org_id=s.org_id,
        workspace_id=ws or s.workspace_id,
        subject_id=s.id,
        predicate=predicate,
        object_id=o.id if o else None,
        object_text=None if o else "text",
        confidence=confidence,
        verification="verified" if verified else "unverified",
        revision_id=uuid.uuid4(),
        published=published,
    )


def evidence_for(c: GClaim) -> tuple[GEvidence, uuid.UUID]:
    chunk = uuid.uuid4()
    return (
        GEvidence(uuid.uuid4(), c.id, chunk, c.revision_id, uuid.uuid4(), uuid.uuid4(), "excerpt"),
        chunk,
    )


def scope_of(*workspaces: uuid.UUID, org: uuid.UUID = ORG):  # type: ignore[no-untyped-def]
    return make_scope(org, workspaces or (WS1,))


def pub_rows(claims_ev: list[tuple[GEvidence, GClaim]]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            revision_id=ev.revision_id,
            document_id=ev.document_id,
            version_id=ev.version_id,
            workspace_id=c.workspace_id,
            version_number=1,
            content_type="text/plain",
            title="Doc",
            workspace_slug="ws",
        )
        for ev, c in claims_ev
    ]


def db_for(rows: list[SimpleNamespace], workspaces: tuple[uuid.UUID, ...] = (WS1,)) -> Any:
    return FakeDb(org_id=ORG, workspaces=workspaces, published=rows).session


__all__ = ["FakeChunkSource", "FakeGraphRead"]
