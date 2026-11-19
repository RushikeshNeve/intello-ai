"""Provenance-aware citation resolution and chunk evidence (M04.01.04, contract m04 §4).

* :func:`resolve_citation` re-checks scope, document liveness and that the cited version (and the
  chunk, through the revision that owns it) still exists. It resolves to the EXACT cited version
  even when a newer one is published; a deleted document or a workspace the user lost never
  resolves.
* :func:`load_version_chunk` reads the chunk text from the artifact store (``chunks.json``,
  sha256-verified against the checkpoint), never from the index, so a stale index cannot leak or
  mislead. A chunk is only ever returned from the revision of the version asked for.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.ingestion.artifacts import ArtifactStore, artifact_key, decode_chunks
from app.modules.ingestion.ids import chunk_id_for
from app.modules.retrieval.scope import RetrievalScope


class ArtifactUnavailable(Exception):
    """The chunk artifact is missing or failed verification (answer 410, nothing leaked)."""


@dataclass(frozen=True, slots=True)
class CitationRef:
    """What a stored citation keeps (no evidence text)."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    marker: str = ""
    evidence_id: str = ""
    page_start: int | None = None
    page_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    excerpt: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedCitation:
    ref: CitationRef
    workspace_id: uuid.UUID
    title: str
    version_number: int
    is_current_version: bool


@dataclass(frozen=True, slots=True)
class ChunkView:
    chunk_id: uuid.UUID
    text: str
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    locators: list[dict[str, Any]]
    prev_chunk_id: uuid.UUID | None
    next_chunk_id: uuid.UUID | None


_DOC_SQL: Final = text(
    "SELECT d.workspace_id, d.title, v.version_number, p.version_id AS current_version_id"
    " FROM documents d"
    " JOIN document_versions v ON v.document_id = d.id AND v.org_id = d.org_id AND v.id = :v"
    " JOIN workspaces w ON w.id = d.workspace_id AND w.org_id = d.org_id"
    " LEFT JOIN document_publications p ON p.document_id = d.id"
    " WHERE d.org_id = :o AND d.id = :d AND d.deleted_at IS NULL AND w.deleted_at IS NULL"
    " AND v.purged_at IS NULL"
)
_REVISIONS_SQL: Final = text(
    "SELECT id, state, chunk_count FROM knowledge_revisions"
    " WHERE org_id = :o AND document_id = :d AND version_id = :v ORDER BY created_at, id"
)
_CHUNK_CHECKPOINT_SQL: Final = text(
    "SELECT artifact_key, artifact_sha256, artifact_bytes FROM ingestion_checkpoints"
    " WHERE org_id = :o AND revision_id = :r AND stage = 'chunk' AND state = 'completed'"
)


def owner_revision(
    chunk_id: uuid.UUID, revisions: Sequence[tuple[uuid.UUID, int]]
) -> tuple[uuid.UUID, int] | None:
    """The ``(revision_id, chunk_index)`` whose deterministic chunk id is ``chunk_id``."""
    for revision_id, chunk_count in revisions:
        for index in range(chunk_count):
            if chunk_id_for(revision_id, index) == chunk_id:
                return revision_id, index
    return None


def _revisions(
    session: Session, org_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID
) -> list[tuple[uuid.UUID, int]]:
    rows = session.execute(_REVISIONS_SQL, {"o": org_id, "d": document_id, "v": version_id}).all()
    return [(r.id, r.chunk_count) for r in rows]


def resolve_citation(
    session: Session, scope: RetrievalScope, ref: CitationRef
) -> ResolvedCitation | None:
    """Resolve to the cited version, or ``None`` (deleted, lost scope, purged, foreign chunk)."""
    row = session.execute(
        _DOC_SQL, {"o": scope.org_id, "d": ref.document_id, "v": ref.version_id}
    ).first()
    if row is None or row.workspace_id not in scope.workspace_ids:
        return None
    revisions = _revisions(session, scope.org_id, ref.document_id, ref.version_id)
    if owner_revision(ref.chunk_id, revisions) is None:
        return None
    return ResolvedCitation(
        ref=ref,
        workspace_id=row.workspace_id,
        title=row.title,
        version_number=row.version_number,
        is_current_version=row.current_version_id == ref.version_id,
    )


def load_version_chunk(
    session: Session,
    artifacts: ArtifactStore,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    chunk_id: uuid.UUID,
) -> ChunkView | None:
    """The chunk from the artifact of the version's own revision.

    ``None``: the chunk does not belong to this version. ``ArtifactUnavailable``: it does, but the
    artifact is missing, fails sha256/size verification or does not parse. A store outage
    (``StorageUnavailable``) propagates. The caller has already authorized the document.
    """
    owner = owner_revision(chunk_id, _revisions(session, org_id, document_id, version_id))
    if owner is None:
        return None
    revision_id, index = owner
    cp = session.execute(_CHUNK_CHECKPOINT_SQL, {"o": org_id, "r": revision_id}).first()
    key = artifact_key(org_id, revision_id, "chunk")
    if cp is None or cp.artifact_key != key:
        raise ArtifactUnavailable
    data = artifacts.get_verified(key, sha256=cp.artifact_sha256, size=cp.artifact_bytes)
    if data is None:
        raise ArtifactUnavailable
    try:
        chunks = decode_chunks(data)
    except (ValueError, TypeError):  # pydantic ValidationError is a ValueError
        raise ArtifactUnavailable from None
    if index >= len(chunks) or chunks[index].chunk_id != chunk_id:
        raise ArtifactUnavailable
    chunk = chunks[index]
    return ChunkView(
        chunk_id=chunk.chunk_id,
        text=chunk.text,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        heading_path=list(chunk.heading_path),
        locators=[loc.model_dump(mode="json", exclude_none=True) for loc in chunk.locators],
        prev_chunk_id=chunks[index - 1].chunk_id if index > 0 else None,
        next_chunk_id=chunks[index + 1].chunk_id if index + 1 < len(chunks) else None,
    )
