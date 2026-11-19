"""Ingestion status API (M03.03.04, contract m03-contracts.md §11).

* ``GET  {ws}/documents/{id}/ingestion``        state + short timeline (``document.read``)
* ``POST {ws}/documents/{id}/ingestion/retry``  re-queue a failed document (``document.write``)
* ``GET  {ws}/documents/{id}/ingestion/events`` SSE for one document (``document.read``)
* ``GET  {ws}/ingestion/events``                SSE for the workspace (workspace members/admins)

All routes follow the M02 catalog conventions: org -> membership -> workspace -> document ->
``decide()``, concealed 404 for non-members and deleted documents, audited denials.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import CONFLICT, Tenant, api_error, not_found
from app.modules.audit import AuditEventType
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.ingestion import events_stream as sse
from app.modules.ingestion import status_service as ing
from app.modules.ingestion.schemas import IngestionRetryAccepted, IngestionStatus

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["ingestion"])

_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


@router.get("/documents/{document_id}/ingestion", response_model=IngestionStatus)
def get_ingestion_status(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=ing.MAX_EVENT_LIMIT)] = ing.DEFAULT_EVENT_LIMIT,
    after: Annotated[int | None, Query(ge=0, le=2**62)] = None,
) -> IngestionStatus:
    """Without ``after``: the newest ``limit`` timeline events; with it, the next page forward."""
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
        _row, grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_READ)
        info = ing.latest_job(session, org_id, workspace_id, document_id)
        summary = ing.summarize(info, can_write=ctx.allowed(Action.DOCUMENT_WRITE, grants))
        if info is None:
            return IngestionStatus(document_id=document_id, **summary.model_dump())
        events, next_after = ing.job_events(
            session, org_id, workspace_id, info.job_id, after=after, limit=limit
        )
        return IngestionStatus(
            **summary.model_dump(),
            document_id=document_id,
            version_id=info.version_id,
            job_id=info.job_id,
            revision_id=info.revision_id,
            attempts=info.attempts,
            max_attempts=info.max_attempts,
            published_at=info.published_at if summary.state == "ready" else None,
            events=events,
            next_after=next_after,
        )


@router.post(
    "/documents/{document_id}/ingestion/retry",
    response_model=IngestionRetryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_ingestion(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> IngestionRetryAccepted:
    """Re-queue the latest version's job when it ``failed`` (or is ``unsupported``); a version that
    never got a job gets one. Anything else (queued, processing, ready, no text, cancelled) is
    ``409 conflict``: a repeated click is rejected rather than queued twice."""
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_WRITE)
        row, grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_WRITE)
        if svc.lock_document(session, ctx, document_id) is None:  # deleted while we waited
            raise not_found()
        if row.latest_version is None:
            raise api_error(status.HTTP_409_CONFLICT, CONFLICT)
        info = ing.latest_job(session, org_id, workspace_id, document_id)
        before = ing.public_state(info)
        if info is None:
            job_id = ing.enqueue_missing(
                session, org_id, workspace_id, document_id, row.latest_version.id
            )
        elif ing.is_retryable(info) and ing.requeue(session, info.job_id):
            job_id = info.job_id
        else:
            raise api_error(status.HTTP_409_CONFLICT, CONFLICT)
        svc.audit(
            tenant,
            session,
            AuditEventType.DOCUMENT_INGESTION_RETRIED,
            ctx=ctx,
            document_id=document_id,
            metadata={
                "version_id": str(row.latest_version.id),
                "job_id": str(job_id),
                "from_state": before,
            },
        )
        fresh = ing.latest_job(session, org_id, workspace_id, document_id)
        return IngestionRetryAccepted(
            job_id=job_id,
            ingestion=ing.summarize(fresh, can_write=ctx.allowed(Action.DOCUMENT_WRITE, grants)),
        )


async def _sse(
    request: Request,
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID | None,
    last_event_id: str | None,
    last_event_id_query: str | None,
) -> StreamingResponse:
    scope = sse.Scope(tenant, org_id, workspace_id, document_id)
    resume = sse.parse_last_event_id(last_event_id, last_event_id_query)
    # The first read runs before the 200 is sent: a denied caller gets a real 404/403.
    first = await run_in_threadpool(
        sse.read, scope, resume, resumed=resume is not None, with_snapshot=True
    )
    return StreamingResponse(
        sse.stream(scope, first, fresh=resume is None),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/documents/{document_id}/ingestion/events")
async def document_ingestion_events(
    request: Request,
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=32)] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id", max_length=32)] = None,
) -> StreamingResponse:
    """SSE for one document (protocol in ``events_stream``). Needs ``document.read`` on it."""
    return await _sse(
        request, tenant, org_id, workspace_id, document_id, last_event_id, last_event_id_query
    )


@router.get("/ingestion/events")
async def workspace_ingestion_events(
    request: Request,
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=32)] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id", max_length=32)] = None,
) -> StreamingResponse:
    """SSE for the whole workspace: only org owners/admins and workspace role holders."""
    return await _sse(
        request, tenant, org_id, workspace_id, None, last_event_id, last_event_id_query
    )
