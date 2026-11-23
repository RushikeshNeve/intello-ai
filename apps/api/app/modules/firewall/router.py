"""``GET/PUT /v1/orgs/{org_id}/firewall-policy`` (M14.01/03).

Any org member reads; only an org ``owner``/``admin`` writes (``org.update``). A non-member gets the
concealed 404 and a plain member a 403 (audited by the shared helper). A change writes
``org.firewall_policy.updated`` (modes and threshold only) in the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, api_error, not_found
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.firewall import policy as fw
from app.modules.organizations import service
from app.modules.organizations.service import OrgAccess

router = APIRouter(prefix="/v1/orgs", tags=["firewall"])

TARGET = "firewall_policy"
INVALID_POLICY = "invalid_policy"
InputMode = Literal["off", "flag", "block"]
OutputMode = Literal["off", "flag", "block", "redact"]


class FirewallPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_user: InputMode
    input_evidence: InputMode
    output: OutputMode
    threshold: float = Field(default=fw.DEFAULT_THRESHOLD, ge=0.05, le=1.0)


class FirewallPolicyOut(BaseModel):
    input_user: str
    input_evidence: str
    output: str
    threshold: float
    is_default: bool
    updated_at: datetime | None
    updated_by: uuid.UUID | None


def _access(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> OrgAccess:
    access = service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    return access


def _out(p: fw.FirewallPolicy) -> FirewallPolicyOut:
    return FirewallPolicyOut(
        input_user=p.input_user,
        input_evidence=p.input_evidence,
        output=p.output,
        threshold=p.threshold,
        is_default=p.is_default,
        updated_at=p.updated_at,
        updated_by=p.updated_by,
    )


@router.get("/{org_id}/firewall-policy", response_model=FirewallPolicyOut)
def get_firewall_policy(tenant: Tenant, org_id: uuid.UUID) -> FirewallPolicyOut:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id).require(Action.ORG_READ, target_type=TARGET)
        return _out(fw.load(session, org_id))


@router.put("/{org_id}/firewall-policy", response_model=FirewallPolicyOut)
def put_firewall_policy(
    tenant: Tenant, org_id: uuid.UUID, body: FirewallPolicyIn
) -> FirewallPolicyOut:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id).require(Action.ORG_UPDATE, target_type=TARGET)
        try:
            fw.validate(body.input_user, body.input_evidence, body.output, body.threshold)
        except fw.InvalidFirewallPolicy:
            raise api_error(422, INVALID_POLICY) from None
        before = fw.load(session, org_id)
        stored = fw.save(
            session,
            org_id,
            input_user=body.input_user,
            input_evidence=body.input_evidence,
            output=body.output,
            threshold=body.threshold,
            updated_by=tenant.user_id,
        )
        record_event(
            session,
            event_type=AuditEventType.FIREWALL_POLICY_UPDATED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type=TARGET,
            target_id=str(org_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata={
                "from": [before.input_user, before.input_evidence, before.output, before.threshold],
                "to": [stored.input_user, stored.input_evidence, stored.output, stored.threshold],
            },
        )
        return _out(stored)
