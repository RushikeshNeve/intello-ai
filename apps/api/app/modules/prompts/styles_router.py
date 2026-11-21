"""Answer-style API (M12.07).

* ``GET/PUT /v1/orgs/{o}/workspaces/{w}/style``  the workspace style (admin writes, members read)
* ``GET/PUT /v1/orgs/{o}/style``                 the org default (owners/admins write, members read)

A GET returns the style that applies (``source``: ``workspace`` | ``org_default`` | ``default``).
Audit metadata is the scope and ids only, never the instruction text.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, api_error, not_found
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.organizations.service import OrgAccess, load_org_access
from app.modules.prompts import styles
from app.modules.workspaces import service as ws_service

router = APIRouter(tags=["prompts"])

TARGET = "workspace_style"
Tone = Literal["neutral", "formal", "friendly", "technical"]
AnswerFormat = Literal["concise", "detailed", "bullets", "table"]
CitationStyle = Literal["numeric", "inline"]


class StyleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: Tone = "neutral"
    answer_format: AnswerFormat = "concise"
    language: str | None = Field(default=None, max_length=40)
    citation_style: CitationStyle = "numeric"
    custom_instructions: str = Field(default="", max_length=styles.CUSTOM_MAX_LENGTH)


class StyleOut(BaseModel):
    source: Literal["workspace", "org_default", "default"]
    tone: str
    answer_format: str
    language: str | None
    citation_style: str
    custom_instructions: str
    updated_at: datetime | None
    updated_by: uuid.UUID | None


def _out(source: str, style: styles.Style, row: dict[str, Any] | None) -> StyleOut:
    return StyleOut(
        source=source,  # type: ignore[arg-type]
        tone=style.tone,
        answer_format=style.answer_format,
        language=style.language,
        citation_style=style.citation_style,
        custom_instructions=style.custom_instructions,
        updated_at=None if row is None else row["updated_at"],
        updated_by=None if row is None else row["updated_by"],
    )


def _org_access(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> OrgAccess:
    access = load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    access.require(Action.ORG_READ, target_type=TARGET, target_id=str(org_id))
    return access


def _ws_access(
    tenant: TenantRequest, session: Session, org_id: uuid.UUID, ws: uuid.UUID
) -> OrgAccess:
    access = _org_access(tenant, session, org_id)
    found = ws_service.load_workspace(session, org_id, ws, tenant.user_id)
    if found is None:
        raise not_found()
    access = replace(access, subject=replace(access.subject, workspace_role=found[1]))
    access.require(Action.WORKSPACE_READ, target_type=TARGET, target_id=str(ws))
    return access


def _validated(body: StyleIn) -> styles.Style:
    try:
        return styles.validate(
            tone=body.tone,
            answer_format=body.answer_format,
            language=body.language,
            citation_style=body.citation_style,
            custom_instructions=body.custom_instructions,
        )
    except styles.StyleRejected:
        raise api_error(422, "invalid_style") from None


def _audit(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, scope: str, target: uuid.UUID
) -> None:
    record_event(
        session,
        event_type=AuditEventType.STYLE_UPDATED,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=TARGET,
        target_id=str(target),
        outcome="success",
        request_id=tenant.request_id,
        metadata={"scope": scope},
    )


def _effective(session: Session, org_id: uuid.UUID, ws: uuid.UUID | None) -> StyleOut:
    if ws is not None:
        row = styles.get_row(session, org_id, ws)
        if row is not None:
            return _out("workspace", styles.to_style(row), row)
    row = styles.get_row(session, org_id, None)
    if row is not None:
        return _out("org_default", styles.to_style(row), row)
    return _out("default", styles.DEFAULT_STYLE, None)


@router.get("/v1/orgs/{org_id}/workspaces/{workspace_id}/style", response_model=StyleOut)
def get_workspace_style(tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID) -> StyleOut:
    with tenant.transaction(org_id) as session:
        _ws_access(tenant, session, org_id, workspace_id)
        return _effective(session, org_id, workspace_id)


@router.put("/v1/orgs/{org_id}/workspaces/{workspace_id}/style", response_model=StyleOut)
def put_workspace_style(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, body: StyleIn
) -> StyleOut:
    style = _validated(body)
    with tenant.transaction(org_id) as session:
        access = _ws_access(tenant, session, org_id, workspace_id)
        access.require(Action.WORKSPACE_UPDATE, target_type=TARGET, target_id=str(workspace_id))
        row = styles.upsert(session, org_id, workspace_id, tenant.user_id, style)
        _audit(session, tenant, org_id, "workspace", workspace_id)
        return _out("workspace", style, row)


@router.get("/v1/orgs/{org_id}/style", response_model=StyleOut)
def get_org_style(tenant: Tenant, org_id: uuid.UUID) -> StyleOut:
    with tenant.transaction(org_id) as session:
        _org_access(tenant, session, org_id)
        return _effective(session, org_id, None)


@router.put("/v1/orgs/{org_id}/style", response_model=StyleOut)
def put_org_style(tenant: Tenant, org_id: uuid.UUID, body: StyleIn) -> StyleOut:
    style = _validated(body)
    with tenant.transaction(org_id) as session:
        access = _org_access(tenant, session, org_id)
        access.require(Action.ORG_UPDATE, target_type=TARGET, target_id=str(org_id))
        row = styles.upsert(session, org_id, None, tenant.user_id, style)
        _audit(session, tenant, org_id, "org_default", org_id)
        return _out("org_default", style, row)
