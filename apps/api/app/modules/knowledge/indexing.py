"""Versioned chunk indexing (M03.03.02, contract §8/§9 steps up to ``ready``).

A **revision** is the immutable unit of indexing: ``revision_id`` is deterministic from the version,
the pipeline version and the embedding space (``ids.revision_id_for``). Every chunk record carries
its ``revision_id`` and is written **unpublished**; two revisions of one document (an old and a new
version, or two embedding spaces) coexist in the index and never touch each other's records: all
index calls are scoped by ``revision_id`` and a re-index of the same revision merges onto the same
deterministic record ids (idempotent, never duplicates, never un-publishes).

Order of the helpers: ``ensure_revision`` (PostgreSQL row, state ``building``) -> ``index_chunks``
(RushDB, unpublished, in slices so a worker can heartbeat and be fenced between slices) ->
``validate_index`` (count and ids) -> ``mark_ready`` (PostgreSQL, ``ready``). Nothing here can
make a chunk searchable: only the publication barrier does (``knowledge/publication.py``).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.ingestion.contracts import Chunk
from app.modules.ingestion.ids import chunk_id_for
from app.modules.ingestion.models import KnowledgeRevision
from app.modules.knowledge.ports import ChunkRecord, KnowledgeRepository, OrgScope

DEFAULT_SLICE_SIZE: Final = 100
STATE_BUILDING: Final = "building"
STATE_READY: Final = "ready"
STATE_PUBLISHED: Final = "published"
STATE_SUPERSEDED: Final = "superseded"
STATE_FAILED: Final = "failed"
STATE_PURGED: Final = "purged"


class IndexValidationError(Exception):
    """The revision in the index is not exactly the revision that should be there."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RevisionIdentity:
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID
    pipeline_version: str
    embedding_space_id: str


# ----------------------------------------------------------------------------- PostgreSQL side


def ensure_revision(session: Session, ident: RevisionIdentity) -> str:
    """Create the ``building`` revision row if absent; return its current state.

    Idempotent (``ON CONFLICT DO NOTHING`` on the primary key). A ``failed`` revision is put back
    to ``building`` so a retry can rebuild it. The caller fences the transaction.
    """
    session.execute(
        pg_insert(KnowledgeRevision)
        .values(
            id=ident.revision_id,
            org_id=ident.org_id,
            workspace_id=ident.workspace_id,
            document_id=ident.document_id,
            version_id=ident.version_id,
            pipeline_version=ident.pipeline_version,
            embedding_space_id=ident.embedding_space_id,
            state=STATE_BUILDING,
        )
        .on_conflict_do_nothing(index_elements=[KnowledgeRevision.id])
    )
    session.execute(
        update(KnowledgeRevision)
        .where(KnowledgeRevision.id == ident.revision_id, KnowledgeRevision.state == STATE_FAILED)
        .values(state=STATE_BUILDING)
    )
    state: str = session.execute(
        select(KnowledgeRevision.state).where(KnowledgeRevision.id == ident.revision_id)
    ).scalar_one()
    return state


def get_revision_state(session: Session, revision_id: uuid.UUID) -> str | None:
    state: str | None = session.execute(
        select(KnowledgeRevision.state).where(KnowledgeRevision.id == revision_id)
    ).scalar_one_or_none()
    return state


def mark_ready(
    session: Session, revision_id: uuid.UUID, *, chunk_count: int, content_fingerprint: str
) -> None:
    """``building`` (or an already ``ready``) revision -> ``ready`` with its count and fingerprint.

    A revision that is already published/superseded/purged is left alone. The caller fences.
    """
    session.execute(
        update(KnowledgeRevision)
        .where(
            KnowledgeRevision.id == revision_id,
            KnowledgeRevision.state.in_((STATE_BUILDING, STATE_READY)),
        )
        .values(
            state=STATE_READY,
            chunk_count=chunk_count,
            content_fingerprint=content_fingerprint,
            ready_at=text("now()"),
        )
    )


# --------------------------------------------------------------------------------- pure helpers


def content_fingerprint(chunks: Sequence[Chunk]) -> str:
    """sha256 over the ordered ``content_hash`` values (contract §9 step 1)."""
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda c: c.chunk_index):
        digest.update(chunk.content_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def chunk_records(ident: RevisionIdentity, chunks: Sequence[Chunk]) -> list[ChunkRecord]:
    """Index records of ``chunks`` for the revision ``ident`` (all unpublished by construction)."""
    records: list[ChunkRecord] = []
    for chunk in chunks:
        records.append(
            ChunkRecord(
                org_id=ident.org_id,
                workspace_id=ident.workspace_id,
                document_id=ident.document_id,
                version_id=ident.version_id,
                revision_id=ident.revision_id,
                chunk_index=chunk.chunk_index,
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                token_estimate=chunk.token_estimate,
                content_hash=chunk.content_hash,
                locators=[loc.model_dump(mode="json", exclude_none=True) for loc in chunk.locators],
                heading_path=list(chunk.heading_path),
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                embedding_space_id=ident.embedding_space_id,
            )
        )
    return records


# ------------------------------------------------------------------------------------ the index


def index_chunks(
    repo: KnowledgeRepository,
    scope: OrgScope,
    ident: RevisionIdentity,
    chunks: Sequence[Chunk],
    vectors: Sequence[Sequence[float]],
    *,
    dimensions: int,
    slice_size: int = DEFAULT_SLICE_SIZE,
    between_slices: Callable[[int, int], None] | None = None,
) -> int:
    """Write the revision's chunks (unpublished) in slices; returns the number written.

    ``ensure_indexes`` first (idempotent; a new embedding space gets its own vector index), then
    ``upsert_chunks`` per slice of ``slice_size``. ``between_slices(done, total)`` runs after each
    slice (the pipeline heartbeats and checks its fence there). A crash after any slice leaves a
    partial but **unpublished** revision: invisible to every search (``published = false``) and
    reused as-is by the retry, because record ids are deterministic and writes merge.
    """
    if len(chunks) != len(vectors):
        raise ValueError("one vector per chunk is required")
    if slice_size < 1:
        raise ValueError("slice_size must be >= 1")
    records = chunk_records(ident, chunks)
    repo.ensure_indexes(scope, ident.embedding_space_id, dimensions)
    done = 0
    for start in range(0, len(records), slice_size):
        end = start + slice_size
        done += repo.upsert_chunks(scope, records[start:end], vectors[start:end])
        if between_slices is not None:
            between_slices(done, len(records))
    return done


def validate_index(
    repo: KnowledgeRepository, scope: OrgScope, ident: RevisionIdentity, chunks: Sequence[Chunk]
) -> None:
    """The index holds exactly this revision's chunks: right count and the deterministic ids.

    The adapter exposes counts and ids, not stored ``content_hash`` values; the content is pinned
    by the deterministic ids (merge writes), the chunks artifact's sha256 and the fingerprint
    recorded on the revision row. Raises :class:`IndexValidationError`.
    """
    expected = [chunk_id_for(ident.revision_id, c.chunk_index) for c in chunks]
    count = repo.count_chunks(scope, ident.revision_id)
    if count != len(expected):
        raise IndexValidationError("index_incomplete", f"{count} of {len(expected)} chunks")
    if repo.list_chunk_ids(scope, ident.revision_id) != expected:
        raise IndexValidationError("index_mismatch", "chunk ids differ from the expected ids")
