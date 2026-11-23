"""``/v1/orgs/{org_id}/security/*`` (M14.07): events, abuse settings and canary documents.

Org owners/admins only: non-members get the concealed 404, plain members 403. Events hold ids and
counts only. The canary flag lives in its own table, so no document API ever shows it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.deps import FORBIDDEN, Tenant, api_error, not_found
from app.core.models import Document
from app.modules.audit import AuditEventType, record_event
from app.modules.firewall import abuse
from app.modules.firewall.abuse_models import AbuseSettings, CanaryDocument, SecurityEvent
from app.modules.organizations import service

router = APIRouter(prefix="/v1/orgs/{org_id}/security", tags=["security"])

Action = Literal["off", "flag", "throttle", "block"]


class EventOut(BaseModel):
    id: uuid.UUID
    kind: str
    severity: str
    user_id: uuid.UUID | None
    run_id: uuid.UUID | None
    document_id: uuid.UUID | None
    counts: dict[str, Any]
    created_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: uuid.UUID | None


class EventList(BaseModel):
    events: list[EventOut]


class SettingsBody(BaseModel):
    action: Action = "flag"
    window_seconds: int = Field(default=300, ge=10, le=86400)
    block_seconds: int = Field(default=900, ge=10, le=86400)
    max_search: int = Field(default=120, ge=1, le=10**6)
    max_chat: int = Field(default=60, ge=1, le=10**6)
    max_evidence_reads: int = Field(default=200, ge=1, le=10**6)
    max_export: int = Field(default=20, ge=1, le=10**6)
    max_distinct_docs: int = Field(default=150, ge=1, le=10**6)


class SettingsOut(SettingsBody):
    is_default: bool = False


class CanaryIn(BaseModel):
    document_id: uuid.UUID


class CanaryOut(BaseModel):
    document_id: uuid.UUID
    created_at: datetime


class CanaryList(BaseModel):
    canaries: list[CanaryOut]


def _admin(session: Any, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    access = service.load_org_access(session, org_id, user_id)
    if access is None or access.role is None:
        raise not_found()
    if access.role not in ("owner", "admin"):
        raise api_error(status.HTTP_403_FORBIDDEN, FORBIDDEN)


def _event(row: SecurityEvent) -> EventOut:
    return EventOut(
        id=row.id,
        kind=row.kind,
        severity=row.severity,
        user_id=row.user_id,
        run_id=row.run_id,
        document_id=row.document_id,
        counts=row.counts,
        created_at=row.created_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
    )


@router.get("/events", response_model=EventList)
def list_events(
    tenant: Tenant,
    org_id: uuid.UUID,
    kind: Literal["abuse_anomaly", "canary_hit"] | None = None,
    acknowledged: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> EventList:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        stmt = select(SecurityEvent).where(SecurityEvent.org_id == org_id)
        if kind is not None:
            stmt = stmt.where(SecurityEvent.kind == kind)
        if acknowledged is True:
            stmt = stmt.where(SecurityEvent.acknowledged_at.is_not(None))
        elif acknowledged is False:
            stmt = stmt.where(SecurityEvent.acknowledged_at.is_(None))
        rows = session.scalars(stmt.order_by(SecurityEvent.created_at.desc()).limit(limit)).all()
        return EventList(events=[_event(r) for r in rows])


@router.post("/events/{event_id}/acknowledge", response_model=EventOut)
def acknowledge(tenant: Tenant, org_id: uuid.UUID, event_id: uuid.UUID) -> EventOut:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        row = session.get(SecurityEvent, event_id)
        if row is None or row.org_id != org_id:
            raise not_found()
        if row.acknowledged_at is None:
            row.acknowledged_at = datetime.now(UTC)
            row.acknowledged_by = tenant.user_id
            session.flush()
            record_event(
                session,
                event_type=AuditEventType.SECURITY_EVENT_ACKNOWLEDGED,
                actor_user_id=tenant.user_id,
                org_id=org_id,
                target_type="security_event",
                target_id=str(event_id),
                outcome="success",
                request_id=tenant.request_id,
            )
        return _event(row)


@router.get("/abuse-settings", response_model=SettingsOut)
def get_settings(tenant: Tenant, org_id: uuid.UUID) -> SettingsOut:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        row = session.get(AbuseSettings, org_id)
        cfg = abuse.to_thresholds(row)
        return SettingsOut(**cfg.__dict__, is_default=row is None)


@router.put("/abuse-settings", response_model=SettingsOut)
def put_settings(tenant: Tenant, org_id: uuid.UUID, body: SettingsBody) -> SettingsOut:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        row = session.get(AbuseSettings, org_id)
        if row is None:
            row = AbuseSettings(org_id=org_id, **body.model_dump())
            session.add(row)
        else:
            for key, value in body.model_dump().items():
                setattr(row, key, value)
        row.updated_by = tenant.user_id
        row.updated_at = datetime.now(UTC)
        session.flush()
        record_event(
            session,
            event_type=AuditEventType.SECURITY_ABUSE_SETTINGS_UPDATED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type="abuse_settings",
            target_id=str(org_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata=body.model_dump(),
        )
    abuse.invalidate_settings(org_id)
    return SettingsOut(**body.model_dump())


@router.get("/canaries", response_model=CanaryList)
def list_canaries(tenant: Tenant, org_id: uuid.UUID) -> CanaryList:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        rows = session.scalars(
            select(CanaryDocument)
            .where(CanaryDocument.org_id == org_id)
            .order_by(CanaryDocument.created_at)
        ).all()
        return CanaryList(
            canaries=[CanaryOut(document_id=r.document_id, created_at=r.created_at) for r in rows]
        )


@router.post("/canaries", response_model=CanaryOut, status_code=201)
def mark_canary(tenant: Tenant, org_id: uuid.UUID, body: CanaryIn) -> CanaryOut:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        doc = session.scalar(
            select(Document.id).where(Document.id == body.document_id, Document.org_id == org_id)
        )
        if doc is None:
            raise not_found()
        row = session.get(CanaryDocument, (org_id, body.document_id))
        if row is None:
            row = CanaryDocument(
                org_id=org_id, document_id=body.document_id, created_by=tenant.user_id
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            record_event(
                session,
                event_type=AuditEventType.SECURITY_CANARY_CHANGED,
                actor_user_id=tenant.user_id,
                org_id=org_id,
                target_type="document",
                target_id=str(body.document_id),
                outcome="success",
                request_id=tenant.request_id,
                metadata={"marked": True},
            )
        return CanaryOut(document_id=row.document_id, created_at=row.created_at)


@router.delete("/canaries/{document_id}", status_code=204)
def unmark_canary(tenant: Tenant, org_id: uuid.UUID, document_id: uuid.UUID) -> Response:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        done = session.execute(
            delete(CanaryDocument)
            .where(CanaryDocument.org_id == org_id, CanaryDocument.document_id == document_id)
            .returning(CanaryDocument.document_id)
        ).first()
        if done is None:
            raise not_found()
        record_event(
            session,
            event_type=AuditEventType.SECURITY_CANARY_CHANGED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type="document",
            target_id=str(document_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata={"marked": False},
        )
    return Response(status_code=204)
