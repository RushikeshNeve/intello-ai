"""Memory API (M07.01.02; contract m07 section 3).

* ``POST   /v1/orgs/{o}/memory/suggestions``  suggest (201, state ``suggested``)
* ``GET    /v1/orgs/{o}/memory``              own + workspace-visible (approved), newest first
* ``GET    /v1/orgs/{o}/memory/{id}``         with evidence re-resolved (``evidence_status``)
* ``POST   /v1/orgs/{o}/memory/{id}/approve`` / ``reject``
* ``DELETE /v1/orgs/{o}/memory/{id}``         owner only (soft delete)

Another user's private memory, and anything in an org the caller is not in, is a concealed 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status

from app.api.deps import KeysetCursor, Tenant, api_error
from app.modules.memory import service
from app.modules.memory.schemas import MemoryList, MemoryOut, SuggestionCreate

router = APIRouter(prefix="/v1/orgs/{org_id}/memory", tags=["memory"])

_CONCEALED: dict[int | str, dict[str, Any]] = {
    404: {"description": "Not found, or not yours (concealed)."}
}


@router.post(
    "/suggestions",
    status_code=status.HTTP_201_CREATED,
    response_model=MemoryOut,
    responses=_CONCEALED,
)
def suggest(tenant: Tenant, org_id: uuid.UUID, body: SuggestionCreate) -> MemoryOut:
    with tenant.transaction(org_id) as session:
        return service.suggest(session, tenant, org_id, body)


@router.get("", response_model=MemoryList, responses=_CONCEALED)
def list_memories(
    tenant: Tenant,
    org_id: uuid.UUID,
    state: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[str | None, Query(max_length=512)] = None,
) -> MemoryList:
    cursor: KeysetCursor | None = None
    if after:
        try:
            cursor = KeysetCursor.decode(after)
        except ValueError:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_cursor") from None
    with tenant.transaction(org_id) as session:
        return service.list_memories(
            session, tenant, org_id, state=state, limit=limit, after=cursor
        )


@router.get("/{memory_id}", response_model=MemoryOut, responses=_CONCEALED)
def get_memory(tenant: Tenant, org_id: uuid.UUID, memory_id: uuid.UUID) -> MemoryOut:
    with tenant.transaction(org_id) as session:
        return service.get_memory(session, tenant, org_id, memory_id)


@router.post("/{memory_id}/approve", response_model=MemoryOut, responses=_CONCEALED)
def approve(tenant: Tenant, org_id: uuid.UUID, memory_id: uuid.UUID) -> MemoryOut:
    with tenant.transaction(org_id) as session:
        return service.approve(session, tenant, org_id, memory_id)


@router.post("/{memory_id}/reject", response_model=MemoryOut, responses=_CONCEALED)
def reject(tenant: Tenant, org_id: uuid.UUID, memory_id: uuid.UUID) -> MemoryOut:
    with tenant.transaction(org_id) as session:
        return service.reject(session, tenant, org_id, memory_id)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_CONCEALED)
def delete_memory(tenant: Tenant, org_id: uuid.UUID, memory_id: uuid.UUID) -> Response:
    with tenant.transaction(org_id) as session:
        service.delete(session, tenant, org_id, memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
