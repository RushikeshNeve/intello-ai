"""Evidence: a candidate that passed PostgreSQL revalidation (contract m04 §3).

:class:`Evidence` can only be created by :func:`revalidate` (guard like ``OrgScope``). One batched
query per request (no N+1) checks that the hit's revision is the published revision of a live
document, that the version/workspace match what the index claimed, and that the workspace is
still in the caller's scope. Anything else is dropped silently and counted.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.modules.retrieval.ranking import RRF_K, Candidate, normalize_score, postprocess
from app.modules.retrieval.scope import RetrievalScope

_MINT: Final = object()

_REVALIDATE_SQL: Final = text(
    "SELECT p.revision_id, p.document_id, p.version_id, p.workspace_id, v.version_number,"
    " v.content_type, d.title, w.slug AS workspace_slug"
    " FROM document_publications p"
    " JOIN knowledge_revisions r ON r.id = p.revision_id AND r.document_id = p.document_id"
    " JOIN documents d ON d.id = p.document_id AND d.org_id = p.org_id"
    " JOIN document_versions v ON v.id = p.version_id AND v.document_id = p.document_id"
    " JOIN workspaces w ON w.id = d.workspace_id AND w.org_id = d.org_id"
    " WHERE p.org_id = :o AND r.state = 'published' AND d.deleted_at IS NULL"
    " AND w.deleted_at IS NULL AND v.purged_at IS NULL AND p.revision_id IN :revs"
).bindparams(bindparam("revs", expanding=True))


@dataclass(frozen=True, slots=True)
class Evidence:
    """Revalidated retrieval result. Do not construct: use :func:`revalidate`."""

    evidence_id: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    text: str
    score: float
    locators: tuple[dict[str, Any], ...]
    heading_path: tuple[str, ...]
    page_start: int | None
    page_end: int | None
    document_version_number: int
    content_type: str
    workspace_slug: str
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError("Evidence can only be created by revalidate()")


def revalidate(
    session: Session,
    scope: RetrievalScope,
    candidates: Sequence[Candidate],
    *,
    n_lists: int = 1,
    k: int = RRF_K,
    max_per_document: int | None = None,
) -> tuple[list[Evidence], int]:
    """Return ``(evidence, dropped)`` keeping the candidates' order; ids are ``e1``, ``e2``...

    With ``max_per_document`` the live candidates are then de-duplicated and diversified (so a
    stale revision's chunk is dropped first and can never shadow a live one). ``dropped`` counts
    only candidates that failed revalidation.

    ``session`` must be a tenant session of the org (app role, or the worker role: only tables
    both can read are used).
    """
    if not candidates:
        return [], 0
    revisions = list({c.hit.revision_id for c in candidates})
    rows = session.execute(_REVALIDATE_SQL, {"o": scope.org_id, "revs": revisions}).all()
    live = {r.revision_id: r for r in rows}
    alive: list[tuple[Candidate, Any]] = []
    dropped = 0
    for cand in candidates:
        hit = cand.hit
        row = live.get(hit.revision_id)
        if (
            row is None
            or row.document_id != hit.document_id
            or row.version_id != hit.version_id
            or row.workspace_id != hit.workspace_id
            or row.workspace_id not in scope.workspace_ids
        ):
            dropped += 1
            continue
        alive.append((cand, row))
    if max_per_document is not None:
        by_chunk = {cand.hit.chunk_id: (cand, row) for cand, row in alive}
        kept = postprocess([c for c, _ in alive], max_per_document)
        alive = [by_chunk[c.hit.chunk_id] for c in kept]
    out: list[Evidence] = []
    for cand, row in alive:
        hit = cand.hit
        out.append(
            Evidence(
                evidence_id=f"e{len(out) + 1}",
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                version_id=hit.version_id,
                revision_id=hit.revision_id,
                workspace_id=hit.workspace_id,
                title=row.title,
                text=hit.text,
                score=normalize_score(cand.score, n_lists, k),
                locators=tuple(hit.locators),
                heading_path=tuple(hit.heading_path),
                page_start=hit.page_start,
                page_end=hit.page_end,
                document_version_number=row.version_number,
                content_type=row.content_type,
                workspace_slug=row.workspace_slug,
                _mint=_MINT,
            )
        )
    return out, dropped
