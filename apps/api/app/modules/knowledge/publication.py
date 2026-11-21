"""Publication barrier and reconciliation (M03.03.03, contract §9, ADR-015).

**The pointer is the authority.** ``document_publications`` names the one revision of a
document that may be retrieved. A revision becomes searchable through exactly one atomic
PostgreSQL transaction (:func:`commit_pointer`); the RushDB ``published`` flags are then aligned
*after* the commit (:func:`flip_flags`), and :func:`reconcile_document` repairs any drift in
either direction by making the flags match the pointer. Because retrieval revalidates every hit with
:func:`is_revision_published`, a window where RushDB lags (or briefly flags two revisions) can never
leak a stale or partial revision.

Ordering rules (mutation-tested):

1. a revision is *never* flagged ``published`` in RushDB before the pointer transaction committed;
2. the pointer transaction is fenced (job ``lease_owner``/``lease_epoch``/``running``), re-checks
   that the document/version still exist under a per-document advisory lock, and never moves the
   pointer to an older ``version_number``;
3. deletion takes the same advisory lock, so a publication and a deletion serialize.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.audit.events import AuditEventType, record_event
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.ingestion.checkpoints import add_job_event, fence
from app.modules.ingestion.models import EVENT_JOB_QUEUED, EVENT_REVISION_PUBLISHED, OutboxEvent
from app.modules.ingestion.runner_types import ClaimedJob, WorkerSessions
from app.modules.knowledge.indexing import (
    STATE_PUBLISHED,
    STATE_PURGED,
    STATE_READY,
    STATE_SUPERSEDED,
)
from app.modules.knowledge.ports import (
    KnowledgeRepository,
    OrgScope,
    UnknownOrganization,
    validated_org_scope,
)

PublishStatus = Literal["published", "already_published", "superseded", "document_deleted"]
ReconcileAction = Literal[
    "ok", "repaired", "reindex_requested", "purged", "busy", "organization_unavailable"
]

_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtextextended('doc-publication:' || :d, 0))"
_TRY_LOCK_SQL: Final = (
    "SELECT pg_try_advisory_xact_lock(hashtextextended('doc-publication:' || :d, 0))"
)


class PublicationError(Exception):
    """The barrier refuses: the revision is not publishable (a bug or a corrupted state)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    status: PublishStatus
    revision_id: uuid.UUID
    previous_revision_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    action: ReconcileAction
    flipped: int = 0
    purged_revisions: int = 0


def lock_document(session: Session, document_id: uuid.UUID, *, wait: bool = True) -> bool:
    """Per-document transaction-scoped advisory lock (publication, deletion and reconcile).

    The worker role cannot ``SELECT ... FOR UPDATE`` the ``documents`` row (it has no UPDATE
    privilege), so serialization uses an advisory lock; they need no privileges.
    """
    if wait:
        session.execute(text(_LOCK_SQL), {"d": str(document_id)})
        return True
    return bool(session.execute(text(_TRY_LOCK_SQL), {"d": str(document_id)}).scalar_one())


# --------------------------------------------------------------------------- the barrier (PG)


def commit_pointer(session: Session, claim: ClaimedJob) -> PublishOutcome:
    """The atomic pointer swap, inside the caller's (single) transaction. The caller commits.

    Steps: fence the job -> lock the document -> abort when the document/version was deleted ->
    lock the revision and the pointer -> (a newer published version wins) -> upsert the pointer,
    supersede the old revision, publish the new one -> audit + outbox.
    """
    rev_id = claim.revision_id
    doc_id = claim.document_id
    fence(session, claim, stage="publish")
    lock_document(session, doc_id)

    ctx = session.execute(
        text(
            "SELECT d.deleted_at, v.purged_at, v.version_number"
            " FROM documents d JOIN document_versions v ON v.document_id = d.id"
            " WHERE d.id = :d AND v.id = :v"
        ),
        {"d": doc_id, "v": claim.version_id},
    ).first()
    if ctx is None or ctx.deleted_at is not None or ctx.purged_at is not None:
        return PublishOutcome("document_deleted", rev_id)
    version_number: int = ctx.version_number

    rev = session.execute(
        text("SELECT state, chunk_count FROM knowledge_revisions WHERE id = :r FOR UPDATE"),
        {"r": rev_id},
    ).first()
    if rev is None:
        raise PublicationError("revision_missing")
    pointer = session.execute(
        text(
            "SELECT revision_id, version_number FROM document_publications"
            " WHERE document_id = :d FOR UPDATE"
        ),
        {"d": doc_id},
    ).first()

    if rev.state in (STATE_SUPERSEDED, STATE_PURGED):
        return PublishOutcome("superseded", rev_id)
    if pointer is not None and pointer.revision_id == rev_id and rev.state == STATE_PUBLISHED:
        return PublishOutcome("already_published", rev_id)
    if rev.state != STATE_READY and not (pointer is not None and pointer.revision_id == rev_id):
        raise PublicationError("revision_not_ready", f"state {rev.state}")
    if pointer is not None and pointer.version_number > version_number:
        # An older version finishing late never regresses the pointer.
        session.execute(
            text(
                "UPDATE knowledge_revisions SET state = 'superseded', superseded_at = now()"
                " WHERE id = :r AND state IN ('ready', 'building')"
            ),
            {"r": rev_id},
        )
        return PublishOutcome("superseded", rev_id)

    previous: uuid.UUID | None = pointer.revision_id if pointer is not None else None
    if previous == rev_id:
        previous = None
    if pointer is None:
        session.execute(
            text(
                "INSERT INTO document_publications (document_id, org_id, workspace_id,"
                " revision_id, version_id, version_number, previous_revision_id, published_at)"
                " VALUES (:d, :o, :w, :r, :v, :n, NULL, now())"
            ),
            {
                "d": doc_id,
                "o": claim.org_id,
                "w": claim.workspace_id,
                "r": rev_id,
                "v": claim.version_id,
                "n": version_number,
            },
        )
    else:
        session.execute(
            text(
                "UPDATE document_publications SET revision_id = :r, version_id = :v,"
                " version_number = :n, previous_revision_id = :prev, published_at = now(),"
                " reconciled_at = NULL WHERE document_id = :d"
            ),
            {
                "d": doc_id,
                "r": rev_id,
                "v": claim.version_id,
                "n": version_number,
                "prev": previous if previous is not None else pointer.previous_revision_id,
            },
        )
    if previous is not None:
        session.execute(
            text(
                "UPDATE knowledge_revisions SET state = 'superseded', superseded_at = now()"
                " WHERE id = :p AND state = 'published'"
            ),
            {"p": previous},
        )
    session.execute(
        text(
            "UPDATE knowledge_revisions SET state = 'published', published_at = now()"
            " WHERE id = :r AND state IN ('ready', 'published')"
        ),
        {"r": rev_id},
    )
    record_event(
        session,
        event_type=AuditEventType.DOCUMENT_VERSION_INDEXED,
        actor_user_id=None,  # a system action: the worker has no user
        org_id=claim.org_id,
        target_type="document",
        target_id=str(doc_id),
        outcome="success",
        request_id=None,
        metadata={
            "revision_id": str(rev_id),
            "version_id": str(claim.version_id),
            "version_number": version_number,
            "previous_revision_id": str(previous) if previous is not None else None,
            "chunk_count": rev.chunk_count,
            "job_id": str(claim.job_id),
        },
    )
    session.execute(
        insert(OutboxEvent).values(
            id=uuid.uuid4(),
            org_id=claim.org_id,
            aggregate_type="document",
            aggregate_id=doc_id,
            event_type=EVENT_REVISION_PUBLISHED,
            payload={
                "document_id": str(doc_id),
                "revision_id": str(rev_id),
                "previous_revision_id": str(previous) if previous is not None else None,
            },
        )
    )
    add_job_event(
        session,
        claim,
        "published",
        stage="publish",
        detail={"revision_id": str(rev_id), "version_number": version_number},
    )
    return PublishOutcome("published", rev_id, previous)


# --------------------------------------------------------------------- the flag flip (RushDB)


def flip_flags(
    repo: KnowledgeRepository,
    scope: OrgScope,
    revision_id: uuid.UUID,
    previous_revision_id: uuid.UUID | None,
) -> None:
    """After the pointer commit: flag the new revision, then unflag the old (both idempotent)."""
    repo.set_published(scope, revision_id, True)
    if previous_revision_id is not None and previous_revision_id != revision_id:
        repo.set_published(scope, previous_revision_id, False)


def publish_revision(
    sessions: WorkerSessions,
    repo: KnowledgeRepository,
    claim: ClaimedJob,
    *,
    check_fence: Callable[[], None] | None = None,
) -> PublishOutcome:
    """Run the barrier: pointer transaction first, RushDB flags after the commit.

    ``KnowledgeStoreUnavailable`` from the flip propagates (the job is retried; the next attempt
    finds the pointer already swapped and only redoes the idempotent flip, and the reconcile sweep
    repairs it independently). ``check_fence`` is an optional ``Callable[[], None]``.
    """
    with sessions(claim.org_id) as session:
        outcome = commit_pointer(session, claim)
    # <- the pointer transaction committed here: this is the publication.
    if outcome.status not in ("published", "already_published"):
        return outcome
    if check_fence is not None:
        check_fence()
    with sessions(claim.org_id) as session:
        scope = validated_org_scope(session, claim.org_id)
        previous = _pointer_previous(session, claim.document_id, claim.revision_id)
    flip_flags(repo, scope, claim.revision_id, outcome.previous_revision_id or previous)
    # A deletion or a newer publication may have slipped in between: the pointer decides.
    with sessions(claim.org_id) as session:
        still = _pointer_revision(session, claim.document_id)
    if still != claim.revision_id:
        reconcile_document(sessions, repo, claim.org_id, claim.document_id)
    else:
        with sessions(claim.org_id) as session:
            session.execute(
                text(
                    "UPDATE document_publications SET reconciled_at = now()"
                    " WHERE document_id = :d AND revision_id = :r"
                ),
                {"d": claim.document_id, "r": claim.revision_id},
            )
    return outcome


def _pointer_revision(session: Session, document_id: uuid.UUID) -> uuid.UUID | None:
    value: uuid.UUID | None = session.execute(
        text("SELECT revision_id FROM document_publications WHERE document_id = :d"),
        {"d": document_id},
    ).scalar_one_or_none()
    return value


def _pointer_previous(
    session: Session, document_id: uuid.UUID, revision_id: uuid.UUID
) -> uuid.UUID | None:
    value: uuid.UUID | None = session.execute(
        text(
            "SELECT previous_revision_id FROM document_publications"
            " WHERE document_id = :d AND revision_id = :r"
        ),
        {"d": document_id, "r": revision_id},
    ).scalar_one_or_none()
    return value


# --------------------------------------------------------------------------- the read side (M04)


def is_revision_published(
    session: Session, org_id: uuid.UUID, document_id: uuid.UUID, revision_id: uuid.UUID
) -> bool:
    """Is ``revision_id`` the live, published revision of a live document? (PostgreSQL authority.)

    Retrieval (M04) calls this for every candidate hit and drops the hit when it is ``False``:
    the RushDB ``published`` flag is only a pre-filter. True only when the pointer names the
    revision, the revision's state is ``published`` and the document is not deleted. The session
    must carry the tenant context; ``org_id`` is also an explicit predicate.
    """
    row = session.execute(
        text(
            "SELECT 1 FROM document_publications p"
            " JOIN knowledge_revisions r ON r.id = p.revision_id AND r.document_id = p.document_id"
            " JOIN documents d ON d.id = p.document_id"
            " WHERE p.org_id = :o AND p.document_id = :d AND p.revision_id = :r"
            " AND r.state = 'published' AND d.deleted_at IS NULL"
        ),
        {"o": org_id, "d": document_id, "r": revision_id},
    ).first()
    return row is not None


# --------------------------------------------------------------------------------- reconciliation


def requeue_revision_job(session: Session, revision_id: uuid.UUID) -> uuid.UUID | None:
    """Self-healing: put the (finished) job of a revision back to ``queued`` with an outbox row.

    ``revision_id`` is unique on ``ingestion_jobs``, so "a new job for that version" (contract §9)
    is the same job re-queued; it re-runs with its checkpoints, and a missing index effect makes
    the ``index`` stage recompute. No-op (``None``) when the job is already queued/running.
    """
    row = session.execute(
        text(
            "UPDATE ingestion_jobs SET state = 'queued', run_after = now(), lease_owner = NULL,"
            " lease_expires_at = NULL, finished_at = NULL, last_error_code = 'reindex_requested',"
            " updated_at = now() WHERE revision_id = :r AND state IN ('succeeded', 'dead')"
            " RETURNING id, org_id, workspace_id, document_id, version_id, attempts"
        ),
        {"r": revision_id},
    ).first()
    if row is None:
        return None
    session.execute(
        text(
            "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id,"
            " version_id, event_type, attempt, detail)"
            " VALUES (:j, :o, :w, :d, :v, 'queued', :a, CAST(:detail AS jsonb))"
        ),
        {
            "j": row.id,
            "o": row.org_id,
            "w": row.workspace_id,
            "d": row.document_id,
            "v": row.version_id,
            "a": row.attempts,
            "detail": '{"reason":"reindex_requested"}',
        },
    )
    session.execute(
        insert(OutboxEvent).values(
            id=uuid.uuid4(),
            org_id=row.org_id,
            aggregate_type="ingestion_job",
            aggregate_id=row.id,
            event_type=EVENT_JOB_QUEUED,
            payload={"job_id": str(row.id)},
        )
    )
    job_id: uuid.UUID = row.id
    return job_id


def reconcile_document(
    sessions: WorkerSessions,
    repo: KnowledgeRepository,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID | None = None,
    *,
    artifacts: ArtifactStore | None = None,
) -> ReconcileResult:
    """Make RushDB match the PostgreSQL pointer for one document (idempotent).

    ``revision_id`` is only a hint from the sweep (``knowledge_next_reconcile_batch``); the live
    pointer is re-read here and always wins. Takes the document's advisory lock without waiting: if
    a publication or deletion is in flight the sweep simply tries again later (``busy``).

    * pointed revision: all its chunks flagged ``published`` (when its chunks are missing or
      incomplete the job is re-queued instead: ``reindex_requested``);
    * every other revision of the document: unflagged; ``superseded`` ones are deleted from the
      index (§9 step 4), marked ``purged`` and their artifacts removed (if ``artifacts`` given);
    * no pointer or a deleted document: nothing may stay flagged; a deleted document is purged.

    Transient index errors (``KnowledgeStoreUnavailable``) propagate: the sweep retries.
    """
    del revision_id  # a hint only
    with sessions(org_id) as session:
        if not lock_document(session, document_id, wait=False):
            return ReconcileResult("busy")
        try:
            scope = validated_org_scope(session, org_id)
        except UnknownOrganization:
            return ReconcileResult("organization_unavailable")
        gone = session.execute(
            text("SELECT deleted_at IS NOT NULL FROM documents WHERE id = :d"), {"d": document_id}
        ).scalar_one_or_none()
        deleted = gone is None or bool(gone)  # not visible at all counts as deleted
        pointer = session.execute(
            text(
                "SELECT p.revision_id, r.state AS state, r.chunk_count AS chunk_count"
                " FROM document_publications p JOIN knowledge_revisions r ON r.id = p.revision_id"
                " WHERE p.document_id = :d"
            ),
            {"d": document_id},
        ).first()
        revisions = session.execute(
            text("SELECT id, state FROM knowledge_revisions WHERE document_id = :d"),
            {"d": document_id},
        ).all()

        flipped = 0
        purged = 0
        action: ReconcileAction = "ok"

        if deleted or (pointer is not None and pointer.state == STATE_PURGED):
            repo.delete_document(scope, document_id)
            if artifacts is not None:
                for rev in revisions:
                    artifacts.delete_revision(org_id, rev.id)
            return ReconcileResult("purged")

        pointer_rev: uuid.UUID | None = pointer.revision_id if pointer is not None else None
        if pointer is not None and pointer.state == STATE_PUBLISHED:
            have = repo.count_chunks(scope, pointer.revision_id)
            if have != pointer.chunk_count:
                requeue_revision_job(session, pointer.revision_id)
                action = "reindex_requested"  # an incomplete revision is never flagged
            else:
                flipped += repo.set_published(scope, pointer.revision_id, True)

        for rev in revisions:
            if rev.id == pointer_rev:
                continue
            if rev.state in (STATE_PUBLISHED, STATE_SUPERSEDED, STATE_READY):  # ready: a standby
                # revision of another embedding space (M12.05) is demoted by a cutover/rollback
                flipped += repo.set_published(scope, rev.id, False)
            if rev.state == STATE_SUPERSEDED:
                repo.delete_revision(scope, rev.id)
                if artifacts is not None:
                    artifacts.delete_revision(org_id, rev.id)
                session.execute(
                    text(
                        "UPDATE knowledge_revisions SET state = 'purged', purged_at = now()"
                        " WHERE id = :r AND state = 'superseded'"
                    ),
                    {"r": rev.id},
                )
                purged += 1
        if pointer is not None and action != "reindex_requested":
            session.execute(
                text(
                    "UPDATE document_publications SET reconciled_at = now() WHERE document_id = :d"
                ),
                {"d": document_id},
            )
        if action == "ok" and (flipped or purged):
            action = "repaired"
        return ReconcileResult(action, flipped=flipped, purged_revisions=purged)


class DefaultReconcileHandler:
    """``ReconcileHandler``: drift repair of one document (flag flip AND purge of superseded
    revisions). Serves ``knowledge.reconcile`` and ``knowledge.revision.published``; idempotent.
    A ``busy`` result (a publication/deletion holds the document lock) needs no retry: the pointer
    transaction resets ``reconciled_at`` so the sweep picks the document up again."""

    def __init__(
        self,
        *,
        sessions: WorkerSessions | None = None,
        repo: KnowledgeRepository | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._sessions = sessions
        self._repo = repo
        self._artifacts = artifacts

    def handle(self, org_id: uuid.UUID, document_id: uuid.UUID) -> None:
        from app.modules.ingestion import wiring

        reconcile_document(
            self._sessions or wiring.default_worker_sessions(),
            self._repo or wiring.default_repository(),
            org_id,
            document_id,
            artifacts=self._artifacts or wiring.default_artifact_store(),
        )


def build_reconcile_handler(settings: Settings) -> DefaultReconcileHandler:
    """Dotted-path factory loaded by the worker runtime (``INGEST_RECONCILE_HANDLER``)."""
    del settings
    return DefaultReconcileHandler()
