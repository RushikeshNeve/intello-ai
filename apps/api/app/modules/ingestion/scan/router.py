"""Admin API of the ingestion scan (M14.02). Workspace admins only (``workspace.update``);
everyone else gets the usual concealed 404/403.

* ``GET/PUT  /v1/orgs/{o}/workspaces/{w}/scan-policy``       ``{"mode": "off|flag|quarantine"}``
* ``GET      /v1/orgs/{o}/workspaces/{w}/documents/{d}/findings``
* ``GET      /v1/orgs/{o}/workspaces/{w}/quarantine``
* ``POST     /v1/orgs/{o}/workspaces/{w}/quarantine/{d}/release``   re-queues indexing
* ``DELETE   /v1/orgs/{o}/workspaces/{w}/quarantine/{d}``           the normal document deletion
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, not_found
from app.core.config import Settings, get_settings
from app.modules.audit import AuditEventType
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.documents.deletion import delete_document
from app.modules.documents.schemas import DeletionAccepted
from app.modules.ingestion.scan import service

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["scan"])

Mode = Literal["off", "flag", "quarantine"]


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Mode


class PolicyOut(BaseModel):
    mode: Mode
    source: Literal["workspace", "org_default", "default"]


class FindingOut(BaseModel):
    version_id: uuid.UUID
    rule_id: str
    severity: Literal["low", "medium", "high"]
    location: str
    preview: str
    created_at: datetime


class QuarantineItem(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    title: str
    findings: int
    max_severity: str
    quarantined_at: datetime


class ReleaseOut(BaseModel):
    requeued: bool


def _admin(
    tenant: TenantRequest, session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> svc.WorkspaceCtx:
    ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.WORKSPACE_UPDATE)
    ctx.require(Action.WORKSPACE_UPDATE, target_type="scan", target_id=str(workspace_id))
    return ctx


@router.get("/scan-policy", response_model=PolicyOut)
def get_policy(tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID) -> PolicyOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        row = session.execute(
            text(
                "SELECT workspace_id, mode FROM document_scan_policies"
                " WHERE org_id = :o AND (workspace_id = :w OR workspace_id IS NULL)"
                " ORDER BY workspace_id NULLS LAST LIMIT 1"
            ),
            {"o": org_id, "w": workspace_id},
        ).first()
    if row is None:
        return PolicyOut(mode=service.DEFAULT_MODE, source="default")
    return PolicyOut(mode=row.mode, source="workspace" if row.workspace_id else "org_default")


@router.put("/scan-policy", response_model=PolicyOut)
def put_policy(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, body: PolicyIn
) -> PolicyOut:
    with tenant.transaction(org_id) as session:
        ctx = _admin(tenant, session, org_id, workspace_id)
        session.execute(
            text(
                "INSERT INTO document_scan_policies (id, org_id, workspace_id, mode, updated_by)"
                " VALUES (:id, :o, :w, :m, :u)"
                " ON CONFLICT (workspace_id) WHERE workspace_id IS NOT NULL"
                " DO UPDATE SET mode = EXCLUDED.mode, updated_by = EXCLUDED.updated_by,"
                " updated_at = now()"
            ),
            {
                "id": uuid.uuid4(),
                "o": org_id,
                "w": workspace_id,
                "m": body.mode,
                "u": tenant.user_id,
            },
        )
        svc.audit(
            tenant,
            session,
            AuditEventType.DOCUMENT_UPDATED,
            ctx=ctx,
            document_id=workspace_id,
            metadata={"action": "scan_policy", "mode": body.mode},
        )
    return PolicyOut(mode=body.mode, source="workspace")


@router.get("/documents/{document_id}/findings", response_model=list[FindingOut])
def get_findings(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> list[FindingOut]:
    with tenant.transaction(org_id) as session:
        ctx = _admin(tenant, session, org_id, workspace_id)
        if svc.fetch_document(session, ctx, document_id) is None:
            raise not_found()
        rows = service.list_findings(session, org_id, workspace_id, document_id)
    return [FindingOut(**r) for r in rows]  # type: ignore[arg-type]


@router.get("/quarantine", response_model=list[QuarantineItem])
def get_quarantine(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[QuarantineItem]:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        rows = service.list_quarantine(session, org_id, workspace_id)
    return [
        QuarantineItem(
            document_id=r.document_id,
            version_id=r.version_id,
            title=r.title,
            findings=r.findings,
            max_severity=r.max_severity,
            quarantined_at=r.quarantined_at,  # type: ignore[arg-type]
        )
        for r in rows
    ]


@router.post("/quarantine/{document_id}/release", response_model=ReleaseOut)
def release_document(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> ReleaseOut:
    with tenant.transaction(org_id) as session:
        ctx = _admin(tenant, session, org_id, workspace_id)
        version_id = service.quarantined_version(session, org_id, workspace_id, document_id)
        if version_id is None:
            raise not_found()
        requeued = service.release(
            session, org_id, workspace_id, document_id, version_id, tenant.user_id
        )
        svc.audit(
            tenant,
            session,
            AuditEventType.DOCUMENT_UPDATED,
            ctx=ctx,
            document_id=document_id,
            metadata={"action": "quarantine_release", "version_id": str(version_id)},
        )
    return ReleaseOut(requeued=requeued)


@router.delete(
    "/quarantine/{document_id}",
    response_model=DeletionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_quarantined(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeletionAccepted:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        if service.quarantined_version(session, org_id, workspace_id, document_id) is None:
            raise not_found()
    return delete_document(tenant, org_id, workspace_id, document_id, settings)
