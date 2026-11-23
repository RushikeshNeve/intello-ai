"""Document deletion handler (M03.03.03, contract §9 "Deletion", ADR-018).

Order matters and is tested: **PostgreSQL first, index second.**

1. One transaction (per-document advisory lock, shared with the publication barrier): every queued
   or running job of the document becomes ``cancelled`` (fencing makes a running worker stop at its
   next write), every revision becomes ``purged``. From this commit on retrieval denies the document
   (``is_revision_published`` needs a ``published`` revision and a live document).
2. Then ``delete_document`` in RushDB and the artifacts of every revision of the document.

The M02 delete endpoint already denied access in PostgreSQL (``documents.deleted_at``); the handler
refuses to act on a document that is **not** deleted (a forged or replayed event must never purge a
live document). Everything is idempotent: a re-delivery repeats no-ops.

Contract deviation (see the handoff): the worker role has no ``DELETE`` grant on
``document_publications``, so the pointer *row* stays and is neutralized instead of removed: its
revision becomes ``purged`` (a pointer to a non-``published`` revision is never live).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.ingestion.runner_types import WorkerSessions
from app.modules.knowledge.ports import (
    KnowledgeRepository,
    UnknownOrganization,
    validated_org_scope,
)
from app.modules.knowledge.publication import lock_document

logger: Final = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeletionResult:
    skipped: bool  # the document is not deleted: nothing was touched
    cancelled_jobs: int = 0
    purged_revisions: int = 0


def deny_in_postgres(
    session: Session, org_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[DeletionResult, list[uuid.UUID]]:
    """Step 1, inside the caller's transaction. Returns the result and the document's revisions."""
    lock_document(session, document_id)
    gone = session.execute(
        text("SELECT deleted_at IS NOT NULL FROM documents WHERE id = :d"), {"d": document_id}
    ).scalar_one_or_none()
    if gone is False:
        return DeletionResult(skipped=True), []
    cancelled = session.execute(
        text(
            "UPDATE ingestion_jobs SET state = 'cancelled', lease_owner = NULL,"
            " lease_expires_at = NULL, finished_at = now(), updated_at = now()"
            " WHERE document_id = :d AND state IN ('queued', 'running')"
            " RETURNING id, workspace_id, version_id, attempts"
        ),
        {"d": document_id},
    ).all()
    for job in cancelled:
        session.execute(
            text(
                "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id,"
                " version_id, event_type, attempt, detail)"
                " VALUES (:j, :o, :w, :d, :v, 'cancelled', :a,"
                ' CAST(\'{"reason":"document_deleted"}\' AS jsonb))'
            ),
            {
                "j": job.id,
                "o": org_id,
                "w": job.workspace_id,
                "d": document_id,
                "v": job.version_id,
                "a": job.attempts,
            },
        )
    session.execute(
        text("DELETE FROM document_findings WHERE document_id = :d"), {"d": document_id}
    )
    revisions = [
        row.id
        for row in session.execute(
            text("SELECT id FROM knowledge_revisions WHERE document_id = :d"), {"d": document_id}
        )
    ]
    purged = len(
        session.execute(
            text(
                "UPDATE knowledge_revisions SET state = 'purged', purged_at = now()"
                " WHERE document_id = :d AND state <> 'purged' RETURNING id"
            ),
            {"d": document_id},
        ).all()
    )
    return DeletionResult(False, len(cancelled), purged), revisions


def purge_document(
    sessions: WorkerSessions,
    repo: KnowledgeRepository,
    artifacts: ArtifactStore | None,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
) -> DeletionResult:
    """Deny in PostgreSQL (committed), then purge the index and the artifacts."""
    with sessions(org_id) as session:
        result, revisions = deny_in_postgres(session, org_id, document_id)
    if result.skipped:
        logger.warning("document.deleted event for a live document ignored")
        return result
    # <- committed: the document is denied everywhere; the rest is cleanup and may be retried.
    try:
        with sessions(org_id) as session:
            scope = validated_org_scope(session, org_id)
    except UnknownOrganization:
        return result  # the organization itself is gone: tenant purge is the org-deletion job
    repo.delete_document(scope, document_id)
    if artifacts is not None:
        for revision_id in revisions:
            artifacts.delete_revision(org_id, revision_id)
    return result


class DefaultDocumentDeletedHandler:
    """``DocumentDeletedHandler`` loaded by the worker as
    ``app.modules.knowledge.deletion:DefaultDocumentDeletedHandler`` (no-argument construction)."""

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

        sessions = self._sessions or wiring.default_worker_sessions()
        repo = self._repo or wiring.default_repository()
        artifacts = self._artifacts or wiring.default_artifact_store()
        purge_document(sessions, repo, artifacts, org_id, document_id)


def build_document_deleted_handler(settings: Settings) -> DefaultDocumentDeletedHandler:
    """Dotted-path factory loaded by the worker runtime (``INGEST_DELETE_HANDLER``)."""
    del settings
    return DefaultDocumentDeletedHandler()
