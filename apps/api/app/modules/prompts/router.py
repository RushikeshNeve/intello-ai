"""Prompt registry admin API (M12.01), org owners/admins only (``org.update``).

* ``GET  /v1/orgs/{o}/prompts?kind=``                   versions (own + global defaults)
* ``POST /v1/orgs/{o}/prompts/{kind}/versions``         create a draft (201)
* ``POST /v1/orgs/{o}/prompts/versions/{id}/activate``  activate an own version
* ``POST /v1/orgs/{o}/prompts/{kind}/rollback``         activate the previous version, or fall back
  to the global default when there is none

Another org's version is a concealed 404. Audit metadata is ids, kind and version numbers only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, api_error, not_found
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.organizations import service as org_service
from app.modules.organizations.service import OrgAccess
from app.modules.prompts import activate as activate_gate
from app.modules.prompts import service
from app.modules.prompts.scaffold import BODY_MAX_LENGTH, BodyRejected, validate_body

router = APIRouter(prefix="/v1/orgs/{org_id}/prompts", tags=["prompts"])

TARGET = "prompt_version"
PromptKind = Literal["chat_system", "graph_extract", "research_plan"]


class PromptVersionOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID | None  # None = global default
    kind: str
    version: int
    body: str
    sha256: str
    change_note: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    state: str
    activated_at: datetime | None
    eval_run_id: uuid.UUID | None = None
    eval_attestation: str | None = None


class ActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: uuid.UUID | None = None
    attestation_id: str | None = Field(default=None, max_length=200)


class GateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enforced: bool
    min_pass_rate: float = Field(default=activate_gate.DEFAULT_MIN_PASS_RATE, ge=0, le=1)


class GateOut(BaseModel):
    enforced: bool
    min_pass_rate: float


class PromptVersionList(BaseModel):
    items: list[PromptVersionOut]


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=BODY_MAX_LENGTH)
    change_note: str | None = Field(default=None, max_length=500)


class RollbackOut(BaseModel):
    reverted_to_default: bool
    active: PromptVersionOut | None


def _access(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> OrgAccess:
    access = org_service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    access.require(Action.ORG_UPDATE, target_type=TARGET)
    return access


def _audit(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    event: AuditEventType,
    target_id: str,
    metadata: dict[str, Any],
) -> None:
    record_event(
        session,
        event_type=event,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=TARGET,
        target_id=target_id,
        outcome="success",
        request_id=tenant.request_id,
        metadata=metadata,
    )


@router.get("", response_model=PromptVersionList)
def list_versions(
    tenant: Tenant, org_id: uuid.UUID, kind: Annotated[PromptKind | None, Query()] = None
) -> PromptVersionList:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        rows = service.list_versions(session, org_id, kind)
    return PromptVersionList(items=[PromptVersionOut(**r) for r in rows])


@router.post(
    "/{kind}/versions", status_code=status.HTTP_201_CREATED, response_model=PromptVersionOut
)
def create_draft(
    tenant: Tenant, org_id: uuid.UUID, kind: PromptKind, body: DraftIn
) -> PromptVersionOut:
    try:
        text = validate_body(body.body)
    except BodyRejected:
        raise api_error(422, "invalid_prompt_body") from None
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        try:
            row = service.create_draft(
                session, org_id, tenant.user_id, kind, text, body.change_note
            )
        except service.VersionConflict:
            raise api_error(status.HTTP_409_CONFLICT, "prompt_version_conflict") from None
        _audit(
            session,
            tenant,
            org_id,
            AuditEventType.PROMPT_VERSION_CREATED,
            str(row["id"]),
            {"kind": kind, "version": row["version"]},
        )
    return PromptVersionOut(**row)


@router.post("/versions/{version_id}/activate", response_model=PromptVersionOut)
def activate(
    tenant: Tenant, org_id: uuid.UUID, version_id: uuid.UUID, body: ActivateIn | None = None
) -> PromptVersionOut:
    body = body or ActivateIn()
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        version = service.get_own(session, org_id, version_id)
        if version is None:
            raise not_found()
        try:
            evidence = (
                (None, None)
                if version["state"] == "active"
                else activate_gate.check_gate(
                    session, org_id, version, body.eval_run_id, body.attestation_id
                )
            )
        except activate_gate.EvalRequired:
            raise api_error(status.HTTP_409_CONFLICT, "eval_required") from None
        try:
            row = service.activate(session, org_id, version, evidence)
        except service.VersionConflict:
            raise api_error(status.HTTP_409_CONFLICT, "prompt_version_conflict") from None
        _audit(
            session,
            tenant,
            org_id,
            AuditEventType.PROMPT_VERSION_ACTIVATED,
            str(row["id"]),
            {"kind": row["kind"], "version": row["version"]},
        )
    return PromptVersionOut(**row)


@router.get("/gate", response_model=GateOut)
def get_gate(tenant: Tenant, org_id: uuid.UUID) -> GateOut:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        s = activate_gate.load_settings(session, org_id)
    return GateOut(enforced=s.enforced, min_pass_rate=s.min_pass_rate)


@router.put("/gate", response_model=GateOut)
def put_gate(tenant: Tenant, org_id: uuid.UUID, body: GateIn) -> GateOut:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        s = activate_gate.save_settings(session, org_id, body.enforced, body.min_pass_rate)
    return GateOut(enforced=s.enforced, min_pass_rate=s.min_pass_rate)


@router.post("/{kind}/rollback", response_model=RollbackOut)
def rollback(tenant: Tenant, org_id: uuid.UUID, kind: PromptKind) -> RollbackOut:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        try:
            had_active, row = service.rollback(session, org_id, kind)
        except service.VersionConflict:
            raise api_error(status.HTTP_409_CONFLICT, "prompt_version_conflict") from None
        if not had_active:
            raise api_error(status.HTTP_409_CONFLICT, "nothing_to_roll_back")
        _audit(
            session,
            tenant,
            org_id,
            AuditEventType.PROMPT_VERSION_ROLLED_BACK,
            str(org_id) if row is None else str(row["id"]),
            {"kind": kind, "to_version": None if row is None else row["version"]},
        )
    return RollbackOut(
        reverted_to_default=row is None, active=None if row is None else PromptVersionOut(**row)
    )
