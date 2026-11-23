"""Research API (M07.02; contract m07 section 4).

* ``POST /v1/orgs/{o}/research``                 start (202 ``{id, state: 'planned'}``)
* ``GET  /v1/orgs/{o}/research``                 own sessions, newest first, keyset
* ``GET  /v1/orgs/{o}/research/{id}``            state, plan, steps, usage, report (when done)
* ``POST /v1/orgs/{o}/research/{id}/cancel``     cancel (idempotent), 202
* ``GET  /v1/orgs/{o}/research/{id}/export?format=md``  the report as markdown (no raw HTML)

Another member's session, and anything in an org the caller is not in, is a concealed 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import KeysetCursor, Tenant, api_error
from app.core.config import Settings, get_settings
from app.modules.firewall import abuse
from app.modules.research import service
from app.modules.research.schemas import (
    CancelOut,
    ResearchAccepted,
    ResearchCreate,
    ResearchList,
    ResearchOut,
)

router = APIRouter(prefix="/v1/orgs/{org_id}/research", tags=["research"])

_CONCEALED: dict[int | str, dict[str, Any]] = {
    404: {"description": "Not found, or not yours (concealed)."}
}


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResearchAccepted,
    responses={**_CONCEALED, 409: {"description": "too_many_research"}},
)
def create(
    tenant: Tenant,
    org_id: uuid.UUID,
    body: ResearchCreate,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResearchAccepted:
    with tenant.transaction(org_id) as session:
        return service.create(session, tenant, org_id, body, settings)


@router.get("", response_model=ResearchList, responses=_CONCEALED)
def list_sessions(
    tenant: Tenant,
    org_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[str | None, Query(max_length=512)] = None,
) -> ResearchList:
    cursor: KeysetCursor | None = None
    if after:
        try:
            cursor = KeysetCursor.decode(after)
        except ValueError:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_cursor") from None
    with tenant.transaction(org_id) as session:
        return service.list_all(session, tenant, org_id, limit=limit, after=cursor)


@router.get("/{session_id}", response_model=ResearchOut, responses=_CONCEALED)
def get_session(tenant: Tenant, org_id: uuid.UUID, session_id: uuid.UUID) -> ResearchOut:
    with tenant.transaction(org_id) as session:
        return service.detail(session, tenant, org_id, session_id)


@router.post(
    "/{session_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CancelOut,
    responses=_CONCEALED,
)
def cancel(tenant: Tenant, org_id: uuid.UUID, session_id: uuid.UUID) -> CancelOut:
    with tenant.transaction(org_id) as session:
        return service.cancel(session, tenant, org_id, session_id)


@router.get("/{session_id}/export", responses=_CONCEALED)
def export(
    tenant: Tenant,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    format: Annotated[Literal["md"], Query()] = "md",
) -> Response:
    with tenant.transaction(org_id) as session:
        body = service.export_markdown(session, tenant, org_id, session_id)
        abuse.guard(tenant.sessions, org_id, tenant.user_id, "export", request_id=tenant.request_id)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="research-{session_id}.md"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
