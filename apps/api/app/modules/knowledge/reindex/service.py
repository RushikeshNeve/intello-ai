"""Blue/green re-embedding of one org into a new embedding space (M12.05, R6).

PostgreSQL is the authority at every step:

* the **job** (``reindex_jobs``) carries state, counts and a lease (``lease_owner``,
  ``lease_expires_at``, ``lease_epoch`` fencing token); every write of a worker is fenced on it,
  so a stale worker raises :class:`LeaseLost` and writes nothing;
* **progress is derived**, not stored as a cursor: a document still needs work while its published
  revision has no ``ready`` sibling (same version and pipeline) in the target space, so a crash at
  any line resumes by simply running the job again (idempotent: deterministic revision/chunk ids,
  merge writes);
* new-space revisions are built **unpublished** (``ready`` rows, ``published = false`` records), so
  search and chat never see a half-built space; only ``reindex_cutover`` (migration 0018) swaps the
  publication pointers and the org's active-space pointer in one transaction.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.ai.embeddings.ports import EmbeddingProvider
from app.modules.ingestion.artifacts import (
    ArtifactStore,
    artifact_key,
    decode_chunks,
    encode_chunks,
)
from app.modules.ingestion.checkpoints import stage_fingerprint
from app.modules.ingestion.ids import chunk_id_for, revision_id_for
from app.modules.ingestion.runner_types import LeaseLost, WorkerSessions
from app.modules.knowledge import indexing
from app.modules.knowledge.indexing import RevisionIdentity
from app.modules.knowledge.ports import (
    KnowledgeRepository,
    UnknownOrganization,
    validated_org_scope,
)

logger: Final = logging.getLogger(__name__)

STATES: Final = ("planned", "running", "verifying", "ready", "cutover", "rolled_back", "failed")
DEFAULT_LEASE_SECONDS: Final = 300
BATCH: Final = 25
MAX_ROUNDS: Final = 3
EMBED_BATCH: Final = 16


class ReindexError(Exception):
    """A job cannot be created or run (stable ``code``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Claim:
    job_id: uuid.UUID
    org_id: uuid.UUID
    owner: str
    epoch: int
    from_space: str
    to_space: str
    to_dimensions: int


@dataclass(frozen=True, slots=True)
class RunResult:
    state: str
    total: int = 0
    done: int = 0
    failed: int = 0


# --------------------------------------------------------------------------- planning (API side)


def current_space(session: Session, org_id: uuid.UUID, default: str) -> str:
    """The org's active embedding space: the PostgreSQL pointer, else ``default`` (config)."""
    found = session.execute(
        text("SELECT space_id FROM org_active_spaces WHERE org_id = :o"), {"o": org_id}
    ).scalar_one_or_none()
    return str(found) if found else default


def create_job(
    session: Session,
    org_id: uuid.UUID,
    *,
    to_space: str,
    to_dimensions: int,
    default_space: str,
    requested_by: uuid.UUID | None,
) -> uuid.UUID:
    """Insert a ``planned`` job. ``from_space`` is the active pointer (else the space most of the
    org's published revisions are in, else ``default_space``)."""
    from_space = current_space(session, org_id, "")
    if not from_space:
        mode = session.execute(
            text(
                "SELECT r.embedding_space_id FROM document_publications p"
                " JOIN knowledge_revisions r ON r.id = p.revision_id"
                " GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1"
            )
        ).scalar_one_or_none()
        from_space = str(mode) if mode else default_space
    if from_space == to_space:
        raise ReindexError("same_space")
    busy = session.execute(
        text(
            "SELECT 1 FROM reindex_jobs WHERE org_id = :o"
            " AND state IN ('planned','running','verifying','ready')"
        ),
        {"o": org_id},
    ).first()
    if busy is not None:
        raise ReindexError("job_active")
    job_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO reindex_jobs (id, org_id, from_space, to_space, to_dimensions,"
            " requested_by) VALUES (:i, :o, :f, :t, :d, :u)"
        ),
        {
            "i": job_id,
            "o": org_id,
            "f": from_space,
            "t": to_space,
            "d": to_dimensions,
            "u": requested_by,
        },
    )
    return job_id


def get_job(session: Session, org_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT id, org_id, from_space, to_space, to_dimensions, state, total, done,"
                " failed,"
                " last_error_code, old_space_purged_at, created_at, started_at, finished_at"
                " FROM reindex_jobs WHERE id = :j AND org_id = :o"
            ),
            {"j": job_id, "o": org_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


# --------------------------------------------------------------------------- lease and fencing


def claim_job(
    sessions: WorkerSessions,
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    owner: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Claim | None:
    """Take the lease (new epoch) of a ``planned``/``running``/``verifying``/``failed`` job whose
    lease is free or expired; ``None`` when another worker holds it or the job is not runnable."""
    with sessions(org_id) as session:
        row = session.execute(
            text(
                "UPDATE reindex_jobs SET state = 'running', lease_owner = :w,"
                " lease_expires_at = now() + make_interval(secs => :s),"
                " lease_epoch = lease_epoch + 1, started_at = COALESCE(started_at, now()),"
                " finished_at = NULL, last_error_code = NULL, updated_at = now()"
                " WHERE id = :j AND org_id = :o"
                " AND state IN ('planned','running','verifying','failed')"
                " AND (lease_owner IS NULL OR lease_expires_at < now())"
                " RETURNING lease_epoch, from_space, to_space, to_dimensions"
            ),
            {"w": owner, "s": lease_seconds, "j": job_id, "o": org_id},
        ).first()
    if row is None:
        return None
    return Claim(job_id, org_id, owner, row[0], row[1], row[2], row[3])


def _fenced_update(
    session: Session, claim: Claim, sets: str, params: dict[str, Any], lease_seconds: int
) -> None:
    """``UPDATE`` the job conditioned on owner+epoch (and extend the lease); no row = lease lost."""
    result = session.execute(
        text(
            f"UPDATE reindex_jobs SET {sets}, updated_at = now(),"  # noqa: S608 - constant `sets`
            " lease_expires_at = now() + make_interval(secs => :lease_s)"
            " WHERE id = :j AND org_id = :o AND lease_owner = :w AND lease_epoch = :e"
            " AND state IN ('running','verifying')"
        ),
        {
            "j": claim.job_id,
            "o": claim.org_id,
            "w": claim.owner,
            "e": claim.epoch,
            "lease_s": lease_seconds,
            **params,
        },
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise LeaseLost


def _fence(sessions: WorkerSessions, claim: Claim, lease_seconds: int) -> None:
    with sessions(claim.org_id) as session:
        _fenced_update(session, claim, "state = state", {}, lease_seconds)


# --------------------------------------------------------------------------------- the worker

_PENDING = """
SELECT p.document_id, p.workspace_id, p.version_id, p.revision_id AS old_rev,
       r0.pipeline_version AS pipeline_version
FROM document_publications p JOIN knowledge_revisions r0 ON r0.id = p.revision_id
WHERE r0.embedding_space_id <> :to AND p.document_id <> ALL(CAST(:skip AS uuid[]))
  AND NOT EXISTS (
      SELECT 1 FROM knowledge_revisions r1
      WHERE r1.version_id = p.version_id AND r1.pipeline_version = r0.pipeline_version
        AND r1.embedding_space_id = :to AND r1.state IN ('ready', 'published'))
ORDER BY p.document_id LIMIT :n
"""

_COUNTS = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM knowledge_revisions r1
           WHERE r1.version_id = p.version_id AND r1.pipeline_version = r0.pipeline_version
             AND r1.embedding_space_id = :to AND r1.state IN ('ready', 'published'))) AS done
FROM document_publications p JOIN knowledge_revisions r0 ON r0.id = p.revision_id
WHERE r0.embedding_space_id <> :to
"""


def run_job(
    sessions: WorkerSessions,
    repo: KnowledgeRepository,
    artifacts: ArtifactStore,
    embedder: EmbeddingProvider,
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    owner: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    claim: Claim | None = None,
) -> RunResult | None:
    """Claim and run (or resume) a job to ``ready`` / ``failed``. ``None`` = nothing to do (the
    lease is held by someone else, or the job is not runnable). Raises :class:`LeaseLost` when this
    worker was fenced out: the caller just stops."""
    claim = claim or claim_job(sessions, org_id, job_id, owner, lease_seconds=lease_seconds)
    if claim is None:
        return None
    if embedder.spec.space_id != claim.to_space:
        return _finish(sessions, claim, "failed", "space_mismatch", lease_seconds)
    skip: set[uuid.UUID] = set()
    problems = 0
    for _round in range(MAX_ROUNDS):
        while True:
            with sessions(org_id) as session:
                batch = session.execute(
                    text(_PENDING),
                    {"to": claim.to_space, "skip": [str(d) for d in skip], "n": BATCH},
                ).all()
            if not batch:
                break
            for row in batch:
                try:
                    _reindex_document(
                        sessions, repo, artifacts, embedder, claim, row, lease_seconds
                    )
                except LeaseLost:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad document must not stop the job
                    logger.warning("reindex document failed: %s", type(exc).__name__)
                    skip.add(row.document_id)
                _progress(sessions, claim, len(skip), lease_seconds)
        with sessions(org_id) as session:
            _fenced_update(session, claim, "state = 'verifying'", {}, lease_seconds)
        problems = verify(sessions, repo, claim)
        if problems == 0 or skip:
            break
        with sessions(org_id) as session:  # a publication moved while verifying: sweep again
            _fenced_update(session, claim, "state = 'running'", {}, lease_seconds)
    if skip:
        return _finish(sessions, claim, "failed", "documents_failed", lease_seconds, len(skip))
    if problems:
        return _finish(sessions, claim, "failed", "verification_failed", lease_seconds)
    return _finish(sessions, claim, "ready", None, lease_seconds)


def _progress(sessions: WorkerSessions, claim: Claim, failed: int, lease_seconds: int) -> None:
    with sessions(claim.org_id) as session:
        counts = session.execute(text(_COUNTS), {"to": claim.to_space}).one()
        _fenced_update(
            session,
            claim,
            "total = :t, done = :d, failed = :f",
            {"t": counts.total, "d": counts.done, "f": failed},
            lease_seconds,
        )


def _finish(
    sessions: WorkerSessions,
    claim: Claim,
    state: str,
    code: str | None,
    lease_seconds: int,
    failed: int = 0,
) -> RunResult:
    with sessions(claim.org_id) as session:
        counts = session.execute(text(_COUNTS), {"to": claim.to_space}).one()
        _fenced_update(
            session,
            claim,
            "state = :st, last_error_code = :c, finished_at = now(), lease_owner = NULL,"
            " total = :t, done = :d, failed = :f",
            {"st": state, "c": code, "t": counts.total, "d": counts.done, "f": failed},
            lease_seconds,
        )
    return RunResult(state, counts.total, counts.done, failed)


def _reindex_document(
    sessions: WorkerSessions,
    repo: KnowledgeRepository,
    artifacts: ArtifactStore,
    embedder: EmbeddingProvider,
    claim: Claim,
    row: Any,
    lease_seconds: int,
) -> None:
    """Re-embed one document's published revision into the target space (idempotent)."""
    new_rev = revision_id_for(row.version_id, row.pipeline_version, claim.to_space)
    with sessions(claim.org_id) as session:
        cp = session.execute(
            text(
                "SELECT artifact_key, artifact_sha256, artifact_bytes FROM ingestion_checkpoints"
                " WHERE revision_id = :r AND stage = 'chunk' AND state = 'completed'"
            ),
            {"r": row.old_rev},
        ).first()
        scope = validated_org_scope(session, claim.org_id)
    if cp is None:
        raise ReindexError("chunks_missing")
    data = artifacts.get_verified(
        cp.artifact_key, sha256=cp.artifact_sha256, size=cp.artifact_bytes
    )
    if data is None:
        raise ReindexError("chunks_missing")
    old_chunks = decode_chunks(data)
    chunks = [
        c.model_copy(update={"chunk_id": chunk_id_for(new_rev, c.chunk_index)}) for c in old_chunks
    ]
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH):
        _fence(sessions, claim, lease_seconds)
        result = embedder.embed_documents_detailed(
            [c.text for c in chunks[start : start + EMBED_BATCH]]
        )
        if len(result.vectors) != len(chunks[start : start + EMBED_BATCH]):
            raise ReindexError("embedding_count_mismatch")
        vectors.extend(result.vectors)
    ident = RevisionIdentity(
        org_id=claim.org_id,
        workspace_id=row.workspace_id,
        document_id=row.document_id,
        version_id=row.version_id,
        revision_id=new_rev,
        pipeline_version=row.pipeline_version,
        embedding_space_id=claim.to_space,
    )
    with sessions(claim.org_id) as session:  # still the published version? (fenced)
        _fenced_update(session, claim, "state = state", {}, lease_seconds)
        pointer = session.execute(
            text("SELECT version_id FROM document_publications WHERE document_id = :d"),
            {"d": row.document_id},
        ).scalar_one_or_none()
        if pointer != row.version_id:
            raise ReindexError("pointer_moved")
        indexing.ensure_revision(session, ident)
    indexing.index_chunks(
        repo,
        scope,
        ident,
        chunks,
        vectors,
        dimensions=claim.to_dimensions,
        between_slices=lambda _d, _t: _fence(sessions, claim, lease_seconds),
    )
    indexing.validate_index(repo, scope, ident, chunks)
    stored = artifacts.put(
        artifact_key(claim.org_id, new_rev, "chunk"),
        encode_chunks(chunks),
        content_type="application/json",
    )
    with sessions(claim.org_id) as session:
        _fenced_update(session, claim, "state = state", {}, lease_seconds)
        session.execute(
            text(
                "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, state,"
                " input_fingerprint, artifact_key, artifact_sha256, artifact_bytes, completed_at)"
                " VALUES (:r, 'chunk', :o, 'completed', :f, :k, :s, :b, now())"
                " ON CONFLICT (revision_id, stage) DO NOTHING"
            ),
            {
                "r": new_rev,
                "o": claim.org_id,
                "k": stored.key,
                "s": stored.sha256,
                "b": stored.size,
                "f": stage_fingerprint("chunk", cp.artifact_sha256, {"reindex": claim.to_space}),
            },
        )
        indexing.mark_ready(
            session,
            new_rev,
            chunk_count=len(chunks),
            content_fingerprint=indexing.content_fingerprint(chunks),
        )


def verify(sessions: WorkerSessions, repo: KnowledgeRepository, claim: Claim) -> int:
    """Counts and ids of every mirrored document, old vs new space; returns the problem count.

    Sample *queries* against the unpublished space are impossible by design (the adapter forces
    ``published = true``); the counts + deterministic-id comparison is the pre-cutover check, and
    ``reindex_cutover`` itself refuses when any pointer document lacks a ``ready`` mirror.
    """
    with sessions(claim.org_id) as session:
        scope = validated_org_scope(session, claim.org_id)
        rows = session.execute(
            text(
                "SELECT p.document_id, r0.id AS old_rev, r0.chunk_count AS old_count,"
                " r1.id AS new_rev, r1.chunk_count AS new_count"
                " FROM document_publications p JOIN knowledge_revisions r0 ON r0.id = p.revision_id"
                " LEFT JOIN knowledge_revisions r1 ON r1.version_id = p.version_id"
                "  AND r1.pipeline_version = r0.pipeline_version AND r1.embedding_space_id = :to"
                "  AND r1.state IN ('ready', 'published')"
                " WHERE r0.embedding_space_id <> :to"
            ),
            {"to": claim.to_space},
        ).all()
    problems = 0
    for r in rows:
        if r.new_rev is None or r.new_count != r.old_count:
            problems += 1
            continue
        expected = [chunk_id_for(r.new_rev, i) for i in range(r.new_count)]
        if repo.count_chunks(scope, r.new_rev) != r.new_count or (
            repo.list_chunk_ids(scope, r.new_rev) != expected
        ):
            problems += 1
    return problems


# ---------------------------------------------------------------------------- explicit old purge


def purge_old_space(
    sessions: WorkerSessions,
    repo: KnowledgeRepository,
    artifacts: ArtifactStore | None,
    org_id: uuid.UUID,
    job_id: uuid.UUID,
) -> int:
    """The only way an old space is deleted: after a ``cutover`` the standby (from-space) revisions,
    after a ``rolled_back`` the abandoned (to-space) ones. Irreversible: rollback is refused after.
    Returns the number of revisions purged."""
    with sessions(org_id) as session:
        job = session.execute(
            text(
                "SELECT state, from_space, to_space, old_space_purged_at FROM reindex_jobs"
                " WHERE id = :j AND org_id = :o"
            ),
            {"j": job_id, "o": org_id},
        ).first()
        if job is None or job.state not in ("cutover", "rolled_back"):
            raise ReindexError("bad_state")
        space = job.from_space if job.state == "cutover" else job.to_space
        scope = validated_org_scope(session, org_id)
        revs: Iterable[Any] = session.execute(
            text(
                "SELECT id FROM knowledge_revisions WHERE org_id = :o"
                " AND embedding_space_id = :s AND state = 'ready'"
            ),
            {"o": org_id, "s": space},
        ).all()
        ids = [r.id for r in revs]
    for rid in ids:
        repo.delete_revision(scope, rid)
        if artifacts is not None:
            artifacts.delete_revision(org_id, rid)
    with sessions(org_id) as session:
        session.execute(
            text(
                "UPDATE knowledge_revisions SET state = 'purged', purged_at = now()"
                " WHERE id = ANY(:ids) AND state = 'ready'"
            ),
            {"ids": ids},
        )
        session.execute(
            text("UPDATE reindex_jobs SET old_space_purged_at = now() WHERE id = :j"),
            {"j": job_id},
        )
    return len(ids)


__all__ = [
    "Claim",
    "ReindexError",
    "RunResult",
    "UnknownOrganization",
    "claim_job",
    "create_job",
    "current_space",
    "get_job",
    "purge_old_space",
    "run_job",
    "verify",
]
