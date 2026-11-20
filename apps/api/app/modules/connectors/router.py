"""Sources API (M06.01; contract m06 section 2). Org admins and workspace admins only."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import Tenant, api_error, not_found
from app.core.config import Settings, get_settings
from app.modules.audit import AuditEventType
from app.modules.connectors import service
from app.modules.connectors.schemas import CreateSource, SourceCreated, SourceOut, SyncAccepted
from app.modules.connectors.webhook import router as webhook_router

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["connectors"])


@router.post("/sources", response_model=SourceCreated, status_code=status.HTTP_201_CREATED)
def create_source(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: CreateSource,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SourceCreated:
    with tenant.transaction(org_id) as session:
        ctx = service.require_admin(tenant, session, org_id, workspace_id)
        return service.create_source(tenant, session, settings, ctx, body)


@router.get("/sources", response_model=list[SourceOut])
def list_sources(tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID) -> list[SourceOut]:
    with tenant.transaction(org_id) as session:
        ctx = service.require_admin(tenant, session, org_id, workspace_id)
        return service.list_sources(session, ctx)


@router.get("/sources/{source_id}", response_model=SourceOut)
def get_source(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, source_id: uuid.UUID
) -> SourceOut:
    with tenant.transaction(org_id) as session:
        ctx = service.require_admin(tenant, session, org_id, workspace_id)
        out = service.read_source(session, ctx, source_id)
        if out is None:
            raise not_found()
        return out


@router.post(
    "/sources/{source_id}/sync", response_model=SyncAccepted, status_code=status.HTTP_202_ACCEPTED
)
def sync_source(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, source_id: uuid.UUID
) -> SyncAccepted:
    with tenant.transaction(org_id) as session:
        ctx = service.require_admin(tenant, session, org_id, workspace_id)
        out = service.read_source(session, ctx, source_id, detail=False)
        if out is None:
            raise not_found()
        if out.state == "revoked":
            raise api_error(status.HTTP_409_CONFLICT, service.SOURCE_REVOKED_CODE)
        sync_id = service.request_sync(session, org_id, source_id, trigger="manual")
        if sync_id is not None:
            service.audit(tenant, session, AuditEventType.SOURCE_SYNC_REQUESTED, ctx, source_id)
        return SyncAccepted(sync_id=sync_id, state="queued" if sync_id else "already_queued")


@router.post("/sources/{source_id}/revoke", response_model=SourceOut)
def revoke_source(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, source_id: uuid.UUID
) -> SourceOut:
    with tenant.transaction(org_id) as session:
        ctx = service.require_admin(tenant, session, org_id, workspace_id)
        return service.revoke_source(tenant, session, ctx, source_id)


__all__ = ["router", "webhook_router"]
