"""Transactional enqueue of ingestion work (M03 contract §2, M03.01.01).

Both functions write **in the caller's transaction** and never commit or flush the session
themselves beyond the statements they execute: if the caller (finalize, delete) rolls back, the
job, its ``queued`` event and the outbox row disappear with the business change (transactional
outbox). The caller's session must already carry the tenant context (``tenant.transaction``), so
every insert passes the row-level-security ``WITH CHECK`` for its own org only.
"""

from __future__ import annotations

import uuid

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.ingestion.ids import PIPELINE_VERSION, revision_id_for
from app.modules.ingestion.models import (
    AGGREGATE_DOCUMENT,
    AGGREGATE_INGESTION_JOB,
    EVENT_DOCUMENT_DELETED,
    EVENT_JOB_QUEUED,
    JOB_STATE_QUEUED,
    IngestionJob,
    IngestionJobEvent,
    OutboxEvent,
)


def enqueue_ingestion(
    session: Session,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    embedding_space_id: str,
    pipeline_version: str = PIPELINE_VERSION,
    max_attempts: int | None = None,
) -> uuid.UUID:
    """Create the ingestion job of one document version, atomically with its outbox row.

    Inserts, in the caller's transaction: the job (``queued``, ``revision_id`` deterministic from
    the version, pipeline and embedding space), a ``queued`` job event and an
    ``ingestion.job.queued`` outbox row. **Idempotent and race-safe:** the unique ``revision_id``
    plus ``ON CONFLICT DO NOTHING`` means a second call for the same revision (a retry, a
    concurrent request) inserts nothing and returns the existing job's id. A concurrent inserter
    blocks on the unique index until the first transaction ends, then either does nothing (the
    first committed) or inserts (the first rolled back).

    ``max_attempts`` defaults to ``INGEST_MAX_ATTEMPTS`` from the settings.
    """
    attempts_limit = (
        max_attempts if max_attempts is not None else get_settings().INGEST_MAX_ATTEMPTS
    )
    revision_id = revision_id_for(version_id, pipeline_version, embedding_space_id)
    job_id = uuid.uuid4()
    inserted = session.execute(
        pg_insert(IngestionJob)
        .values(
            id=job_id,
            org_id=org_id,
            workspace_id=workspace_id,
            document_id=document_id,
            version_id=version_id,
            revision_id=revision_id,
            pipeline_version=pipeline_version,
            embedding_space_id=embedding_space_id,
            state=JOB_STATE_QUEUED,
            max_attempts=attempts_limit,
        )
        .on_conflict_do_nothing(index_elements=[IngestionJob.revision_id])
        .returning(IngestionJob.id)
    ).scalar_one_or_none()
    if inserted is None:
        existing = session.execute(
            select(IngestionJob.id).where(IngestionJob.revision_id == revision_id)
        ).scalar_one()
        return existing

    session.execute(
        insert(IngestionJobEvent).values(
            job_id=job_id,
            org_id=org_id,
            workspace_id=workspace_id,
            document_id=document_id,
            version_id=version_id,
            event_type="queued",
            attempt=0,
            detail={"revision_id": str(revision_id)},
        )
    )
    session.execute(
        insert(OutboxEvent).values(
            id=uuid.uuid4(),
            org_id=org_id,
            aggregate_type=AGGREGATE_INGESTION_JOB,
            aggregate_id=job_id,
            event_type=EVENT_JOB_QUEUED,
            payload={"job_id": str(job_id)},
        )
    )
    return job_id


def enqueue_document_deleted(
    session: Session,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
) -> uuid.UUID:
    """Insert the ``document.deleted`` outbox row in the caller's transaction; returns its id.

    The handler (M03.03.03) cancels the document's jobs, purges its chunks from the knowledge
    index and removes the publication pointer. The payload carries ``document_id`` as the contract
    says, plus ``workspace_id`` (additive) so a handler needs no extra lookup.
    """
    event_id = uuid.uuid4()
    session.execute(
        insert(OutboxEvent).values(
            id=event_id,
            org_id=org_id,
            aggregate_type=AGGREGATE_DOCUMENT,
            aggregate_id=document_id,
            event_type=EVENT_DOCUMENT_DELETED,
            payload={"document_id": str(document_id), "workspace_id": str(workspace_id)},
        )
    )
    return event_id
