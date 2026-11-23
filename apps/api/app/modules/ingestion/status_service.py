"""Read model of the ingestion status (M03.03.04): state mapping, summaries, timeline, retry.

Private to ``app.modules.ingestion.router`` and ``app.modules.documents.catalog_service``. Every
function takes a tenant session (the request's RLS-scoped app-role transaction) and filters by the
path org/workspace explicitly. State always comes from PostgreSQL.

State mapping (the *latest version* of a document; contract §11 with the M03 pipeline outcomes):

======================================  ===============================================
job ``state``                           user-safe state
======================================  ===============================================
(no job)                                ``none``
``queued`` (also "waiting to retry")    ``queued``
``running``                             ``processing``
``succeeded``                           ``ready`` (``processing`` while the revision is not yet
                                        ``published``: only possible mid-publication)
``dead``                                ``failed``
``unsupported`` + ``no_text_extracted`` ``no_text``
``unsupported`` (other)                 ``unsupported``
``cancelled``                           ``cancelled``
======================================  ===============================================
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.models import Document
from app.modules.ingestion.jobs import enqueue_ingestion
from app.modules.ingestion.models import IngestionJob, IngestionJobEvent, KnowledgeRevision
from app.modules.ingestion.schemas import (
    EVENT_LABELS,
    TERMINAL_STATES,
    IngestionEventOut,
    IngestionState,
    IngestionSummary,
    error_message,
    safe_error_code,
)

NO_TEXT_CODE: Final = "no_text_extracted"
DEFAULT_EVENT_LIMIT: Final = 50
MAX_EVENT_LIMIT: Final = 200
_PENDING_REVISION_STATES: Final = frozenset({"building", "ready", "quarantined"})


@dataclass(frozen=True)
class JobInfo:
    """The newest job of a document's latest version plus its revision's progress."""

    document_id: uuid.UUID
    version_id: uuid.UUID
    job_id: uuid.UUID
    revision_id: uuid.UUID
    job_state: str
    stage: str | None
    attempts: int
    max_attempts: int
    error_code: str | None
    updated_at: datetime
    revision_state: str | None
    published_at: datetime | None


def public_state(info: JobInfo | None) -> IngestionState:
    if info is None:
        return "none"
    state = info.job_state
    if state == "queued":
        return "queued"
    if state == "running":
        return "processing"
    if state == "succeeded":
        return "processing" if info.revision_state in _PENDING_REVISION_STATES else "ready"
    if state == "dead":
        return "failed"
    if state == "unsupported":
        return "no_text" if info.error_code == NO_TEXT_CODE else "unsupported"
    return "cancelled"


def is_retryable(info: JobInfo | None) -> bool:
    """Mirrors ``ingest_requeue_job``: dead, or unsupported for a reason other than no text."""
    if info is None:
        return False
    return info.job_state == "dead" or (
        info.job_state == "unsupported" and info.error_code != NO_TEXT_CODE
    )


def _attempts_label(info: JobInfo, state: IngestionState) -> str | None:
    if state == "queued" and info.attempts > 0:
        return (
            f"Retrying: attempt {min(info.attempts + 1, info.max_attempts)} of {info.max_attempts}"
        )
    if state == "processing" and info.attempts > 1:
        return f"Attempt {info.attempts} of {info.max_attempts}"
    return None


def summarize(info: JobInfo | None, *, can_write: bool = False) -> IngestionSummary:
    state = public_state(info)
    if info is None:
        return IngestionSummary(state="none")
    terminal_failure = state in ("failed", "unsupported", "no_text")
    code = info.error_code if terminal_failure else None
    return IngestionSummary(
        state=state,
        stage=info.stage if state in ("queued", "processing") else None,
        attempts_label=_attempts_label(info, state),
        error_code=safe_error_code(code),
        message=error_message(code),
        updated_at=info.updated_at,
        can_retry=can_write and is_retryable(info),
    )


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def _info(row: object) -> JobInfo:
    m = row._mapping  # type: ignore[attr-defined]
    return JobInfo(
        document_id=m["document_id"],
        version_id=m["version_id"],
        job_id=m["id"],
        revision_id=m["revision_id"],
        job_state=m["state"],
        stage=m["current_stage"],
        attempts=m["attempts"],
        max_attempts=m["max_attempts"],
        error_code=m["last_error_code"],
        updated_at=m["updated_at"],
        revision_state=m["rev_state"],
        published_at=m["rev_published_at"],
    )


def latest_jobs(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, JobInfo]:
    """One query for any number of documents: the newest job of each document's latest version.

    Deleted documents and documents of another workspace yield nothing.
    """
    if not document_ids:
        return {}
    j = IngestionJob
    stmt = (
        select(
            j.id,
            j.document_id,
            j.version_id,
            j.revision_id,
            j.state,
            j.current_stage,
            j.attempts,
            j.max_attempts,
            j.last_error_code,
            j.updated_at,
            KnowledgeRevision.state.label("rev_state"),
            KnowledgeRevision.published_at.label("rev_published_at"),
        )
        .join(
            Document,
            and_(
                Document.id == j.document_id,
                Document.org_id == j.org_id,
                Document.latest_version_id == j.version_id,
            ),
        )
        .outerjoin(KnowledgeRevision, KnowledgeRevision.id == j.revision_id)
        .where(
            j.org_id == org_id,
            j.workspace_id == workspace_id,
            j.document_id.in_(list(document_ids)),
            Document.deleted_at.is_(None),
        )
        .ext(distinct_on(j.document_id))
        .order_by(j.document_id, j.created_at.desc(), j.id)
    )
    return {info.document_id: info for info in map(_info, session.execute(stmt).all())}


def latest_job(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> JobInfo | None:
    return latest_jobs(session, org_id, workspace_id, [document_id]).get(document_id)


# --- timeline -----------------------------------------------------------------------------


def _event_out(row: object) -> IngestionEventOut:
    m = row._mapping  # type: ignore[attr-defined]
    return IngestionEventOut(
        id=m["seq"],
        type=m["event_type"],
        label=EVENT_LABELS.get(m["event_type"], "Update"),
        stage=m["stage"],
        attempt=m["attempt"],
        at=m["at"],
    )


_EVENT_COLUMNS = (
    IngestionJobEvent.seq,
    IngestionJobEvent.event_type,
    IngestionJobEvent.stage,
    IngestionJobEvent.attempt,
    IngestionJobEvent.at,
)


def job_events(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    after: int | None,
    limit: int,
) -> tuple[list[IngestionEventOut], int | None]:
    """The job's timeline, oldest first. No ``after``: the newest ``limit`` events (``next`` is
    ``None``). With ``after``: the next page forward and the seq to continue from (or ``None``)."""
    e = IngestionJobEvent
    base = select(*_EVENT_COLUMNS).where(
        e.job_id == job_id, e.org_id == org_id, e.workspace_id == workspace_id
    )
    if after is None:
        rows = session.execute(base.order_by(e.seq.desc()).limit(limit)).all()
        return [_event_out(r) for r in reversed(rows)], None
    rows = session.execute(base.where(e.seq > after).order_by(e.seq).limit(limit + 1)).all()
    page = [_event_out(r) for r in rows[:limit]]
    return page, (page[-1].id if len(rows) > limit and page else None)


@dataclass(frozen=True)
class StreamEvent:
    seq: int
    document_id: uuid.UUID
    version_id: uuid.UUID
    out: IngestionEventOut


def stream_events_after(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    after: int,
    limit: int,
    document_id: uuid.UUID | None,
) -> list[StreamEvent]:
    """Events of a document (or of every live document of the workspace) with ``seq > after``."""
    e = IngestionJobEvent
    stmt = (
        select(*_EVENT_COLUMNS, e.document_id, e.version_id)
        .join(Document, and_(Document.id == e.document_id, Document.org_id == e.org_id))
        .where(
            e.org_id == org_id,
            e.workspace_id == workspace_id,
            e.seq > after,
            Document.deleted_at.is_(None),
        )
        .order_by(e.seq)
        .limit(limit)
    )
    if document_id is not None:
        stmt = stmt.where(e.document_id == document_id)
    out: list[StreamEvent] = []
    for row in session.execute(stmt).all():
        m = row._mapping
        out.append(StreamEvent(m["seq"], m["document_id"], m["version_id"], _event_out(row)))
    return out


def max_event_seq(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID | None,
) -> int:
    e = IngestionJobEvent
    stmt = select(func.coalesce(func.max(e.seq), 0)).where(
        e.org_id == org_id, e.workspace_id == workspace_id
    )
    if document_id is not None:
        stmt = stmt.where(e.document_id == document_id)
    return int(session.execute(stmt).scalar_one())


# --- retry --------------------------------------------------------------------------------


def requeue(session: Session, job_id: uuid.UUID) -> bool:
    """``ingest_requeue_job`` (definer): ``True`` when the job was re-queued by this call."""
    row = session.execute(
        text("SELECT job_id FROM ingest_requeue_job(:job_id)"), {"job_id": job_id}
    ).first()
    return row is not None


def enqueue_missing(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> uuid.UUID:
    """A version that never got a job (uploaded before M03): create it. Idempotent per revision."""
    return enqueue_ingestion(
        session,
        org_id=org_id,
        workspace_id=workspace_id,
        document_id=document_id,
        version_id=version_id,
        embedding_space_id=get_settings().embedding_space_id,
    )
