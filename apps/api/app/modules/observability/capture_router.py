"""Capture admin API (M13.02), org owners/admins only (``org.update``); others get the audited
concealed 404/403.

* ``GET/PUT /v1/orgs/{o}/ai-captures/settings``  mode (off|redacted|full_admin_only) + retention
* ``GET     /v1/orgs/{o}/ai-captures``           metadata list (no text)
* ``GET     /v1/orgs/{o}/ai-captures/{id}``      one capture with its (redacted) text
* ``DELETE  /v1/orgs/{o}/ai-captures/{id}``
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, not_found
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.observability import capture
from app.modules.organizations import service as org_service

router = APIRouter(prefix="/v1/orgs/{org_id}/ai-captures", tags=["observability"])

TARGET = "ai_capture"


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_mode: Literal["off", "redacted", "full_admin_only"] | None = None
    capture_retention_days: int | None = Field(default=None, ge=1, le=capture.MAX_RETENTION_DAYS)


class SettingsOut(BaseModel):
    capture_mode: str
    capture_retention_days: int


class CaptureMeta(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID | None
    kind: str
    mode: str
    prompt_version_id: uuid.UUID | None
    chunk_ids: list[uuid.UUID]
    created_at: datetime
    expires_at: datetime


class CaptureDetail(CaptureMeta):
    messages: str
    answer: str


class CaptureList(BaseModel):
    items: list[CaptureMeta]


def _admin(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> None:
    access = org_service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    access.require(Action.ORG_UPDATE, target_type=TARGET)


def _audit(
    tenant: TenantRequest, session: Session, org_id: uuid.UUID, kind: AuditEventType, target: str
) -> None:
    record_event(
        session,
        event_type=kind,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=TARGET,
        target_id=target,
        outcome="success",
        request_id=tenant.request_id,
        metadata={},
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings_(tenant: Tenant, org_id: uuid.UUID) -> SettingsOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        cfg = capture.load_settings(session, org_id)
    return SettingsOut(capture_mode=cfg.mode, capture_retention_days=cfg.retention_days)


@router.put("/settings", response_model=SettingsOut)
def put_settings(tenant: Tenant, org_id: uuid.UUID, body: SettingsIn) -> SettingsOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        cfg = capture.save_settings(
            session,
            org_id,
            mode=body.capture_mode,
            retention_days=body.capture_retention_days,
            updated_by=tenant.user_id,
        )
        _audit(tenant, session, org_id, AuditEventType.ORG_AI_POLICY_UPDATED, str(org_id))
    return SettingsOut(capture_mode=cfg.mode, capture_retention_days=cfg.retention_days)


@router.get("", response_model=CaptureList)
def list_captures(
    tenant: Tenant, org_id: uuid.UUID, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> CaptureList:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        rows = capture.list_captures(session, org_id, limit)
    return CaptureList(items=[CaptureMeta(**r) for r in rows])


@router.get("/{capture_id}", response_model=CaptureDetail)
def get_capture(tenant: Tenant, org_id: uuid.UUID, capture_id: uuid.UUID) -> CaptureDetail:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        row = capture.get_capture(session, org_id, capture_id)
    if row is None:
        raise not_found()
    return CaptureDetail(**row)


@router.delete("/{capture_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_capture(tenant: Tenant, org_id: uuid.UUID, capture_id: uuid.UUID) -> None:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        if not capture.delete_capture(session, org_id, capture_id):
            raise not_found()
