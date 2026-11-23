"""Workspace ingestion jobs list and counts (M09 backend gap 2).

``GET {ws}/ingestion/jobs?state=&cursor=&limit=``: the newest job of each live document's latest
version (same mapping as ``status_service``), newest update first, plus ``counts`` by state. Both
come from PostgreSQL under the request's RLS transaction and only cover documents the caller can
read (workspace members/admins: all; a grant-only user: the granted documents).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal, get_args

from fastapi import APIRouter, Query, status
from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.api.deps import INVALID_CURSOR, Tenant, TenantRequest, api_error
from app.core.models import Document
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.ingestion.models import IngestionJob, KnowledgeRevision
from app.modules.ingestion.schemas import error_message, safe_error_code
from app.modules.ingestion.status_service import NO_TEXT_CODE

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["ingestion"])

JobState = Literal["queued", "processing", "ready", "failed", "unsupported", "no_text", "cancelled"]
JOB_STATES: Final = get_args(JobState)
_FAILURES: Final = frozenset({"failed", "unsupported", "no_text"})
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200
_SCOPE: Final = "jobs:updated_at:desc"


class JobItem(BaseModel):
    document_id: uuid.UUID
    title: str
    version_id: uuid.UUID
    state: JobState
    stage: str | None
    error_code: str | None
    message: str | None
    attempts: int
    updated_at: datetime


class JobsList(BaseModel):
    items: list[JobItem]
    counts: dict[JobState, int]
    next_cursor: str | None


def _state_expr() -> Any:
    j = IngestionJob
    return case(
        (j.state == "queued", "queued"),
        (j.state == "running", "processing"),
        (
            j.state == "succeeded",
            case(
                (KnowledgeRevision.state.in_(("building", "ready", "quarantined")), "processing"),
                else_="ready",
            ),
        ),
        (j.state == "dead", "failed"),
        (
            j.state == "unsupported",
            case((j.last_error_code == NO_TEXT_CODE, "no_text"), else_="unsupported"),
        ),
        else_="cancelled",
    )


def visible_latest_jobs(ctx: svc.WorkspaceCtx) -> Select[Any]:
    """One row per visible live document (newest job of its latest version) with its user state."""
    j = IngestionJob
    stmt = (
        select(
            j.document_id,
            Document.title.label("title"),
            j.version_id,
            j.current_stage.label("stage"),
            j.attempts,
            j.last_error_code.label("error_code"),
            j.updated_at,
            _state_expr().label("state"),
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
            j.org_id == ctx.org_id,
            j.workspace_id == ctx.workspace_id,
            Document.deleted_at.is_(None),
        )
        .ext(distinct_on(j.document_id))
        .order_by(j.document_id, j.created_at.desc(), j.id)
    )
    if not ctx.sees_every_document:
        stmt = stmt.where(svc._visibility_filter(ctx))
    return stmt


def state_counts(session: Session, ctx: svc.WorkspaceCtx) -> dict[JobState, int]:
    latest = visible_latest_jobs(ctx).subquery("latest")
    rows = session.execute(select(latest.c.state, func.count()).group_by(latest.c.state)).all()
    counts: dict[JobState, int] = dict.fromkeys(JOB_STATES, 0)
    for state, n in rows:
        counts[state] = int(n)
    return counts


def require_readable_workspace(
    tenant: TenantRequest, session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> svc.WorkspaceCtx:
    """Org -> membership -> workspace, then the catalog rule for a role-less caller."""
    ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
    if not ctx.sees_every_document and not svc.has_visible_document(session, ctx):
        ctx.require(
            Action.DOCUMENT_READ, target_type=svc.TARGET_WORKSPACE, target_id=str(workspace_id)
        )
    return ctx


@router.get("/ingestion/jobs", response_model=JobsList)
def list_jobs(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    state: JobState | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> JobsList:
    after: svc.PageCursor | None = None
    stamp: datetime | None = None
    if cursor:
        try:
            after = svc.PageCursor.decode(cursor, _SCOPE)
            stamp = datetime.fromisoformat(after.key)
            if stamp.tzinfo is None:
                raise ValueError("naive timestamp")
        except ValueError:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None
    with tenant.transaction(org_id) as session:
        ctx = require_readable_workspace(tenant, session, org_id, workspace_id)
        counts = state_counts(session, ctx)
        latest = visible_latest_jobs(ctx).subquery("latest")
        stmt = select(latest)
        if state is not None:
            stmt = stmt.where(latest.c.state == state)
        if after is not None and stamp is not None:
            stmt = stmt.where(tuple_(latest.c.updated_at, latest.c.document_id) < (stamp, after.id))
        stmt = stmt.order_by(latest.c.updated_at.desc(), latest.c.document_id.desc()).limit(
            limit + 1
        )
        rows = session.execute(stmt).all()
    page = rows[:limit]
    items = []
    for r in page:
        failure = r.state in _FAILURES
        items.append(
            JobItem(
                document_id=r.document_id,
                title=r.title,
                version_id=r.version_id,
                state=r.state,
                stage=r.stage if r.state in ("queued", "processing") else None,
                error_code=safe_error_code(r.error_code) if failure else None,
                message=error_message(r.error_code) if failure else None,
                attempts=r.attempts,
                updated_at=r.updated_at,
            )
        )
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = svc.PageCursor(_SCOPE, last.updated_at.isoformat(), last.document_id).encode()
    return JobsList(items=items, counts=counts, next_cursor=next_cursor)
