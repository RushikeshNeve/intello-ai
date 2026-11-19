"""``GET/PUT /v1/orgs/{org_id}/ai-policy`` (M04.02.01; contract m04 §5).

Any org member reads; only an org ``owner``/``admin`` writes (``org.update``). A non-member gets the
concealed 404 and a plain member a 403, both audited as ``authz.denied`` by the shared helper. A
change writes ``org.ai_policy.updated`` in the same transaction (ids/flags only).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, api_error, not_found
from app.core.config import Settings, get_settings
from app.modules.ai import policy_store
from app.modules.ai.models_db import AI_PROVIDERS
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.organizations import service
from app.modules.organizations.service import OrgAccess

router = APIRouter(prefix="/v1/orgs", tags=["ai-policy"])

TARGET_AI_POLICY = "ai_policy"
INVALID_POLICY = "invalid_policy"


class AiPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_external: bool
    allowed_providers: list[str] = Field(min_length=1, max_length=len(AI_PROVIDERS))


class AiPolicyOut(BaseModel):
    allow_external: bool
    allowed_providers: list[str]
    is_default: bool
    updated_at: datetime | None
    updated_by: uuid.UUID | None
    # Informational: even a permissive org policy has no effect while the server flag is off.
    server_allows_external: bool


def _access(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> OrgAccess:
    access = service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    return access


def _out(stored: policy_store.StoredPolicy, settings: Settings) -> AiPolicyOut:
    return AiPolicyOut(
        allow_external=stored.policy.allow_external,
        allowed_providers=sorted(stored.policy.allowed_providers),
        is_default=stored.is_default,
        updated_at=stored.updated_at,
        updated_by=stored.updated_by,
        server_allows_external=settings.AI_EXTERNAL_PROVIDERS_ENABLED,
    )


@router.get("/{org_id}/ai-policy", response_model=AiPolicyOut)
def get_ai_policy(
    tenant: Tenant, org_id: uuid.UUID, settings: Annotated[Settings, Depends(get_settings)]
) -> AiPolicyOut:
    with tenant.transaction(org_id) as session:
        access = _access(tenant, session, org_id)
        access.require(Action.ORG_READ, target_type=TARGET_AI_POLICY)
        stored = policy_store.load_stored_policy(session, org_id)
    return _out(stored, settings)


@router.put("/{org_id}/ai-policy", response_model=AiPolicyOut)
def put_ai_policy(
    tenant: Tenant,
    org_id: uuid.UUID,
    body: AiPolicyIn,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AiPolicyOut:
    with tenant.transaction(org_id) as session:
        access = _access(tenant, session, org_id)
        access.require(Action.ORG_UPDATE, target_type=TARGET_AI_POLICY)
        try:
            policy_store.validate_policy(body.allow_external, body.allowed_providers)
        except policy_store.InvalidPolicy:
            raise api_error(422, INVALID_POLICY) from None
        before = policy_store.load_stored_policy(session, org_id)
        stored = policy_store.save_policy(
            session,
            org_id,
            allow_external=body.allow_external,
            allowed_providers=body.allowed_providers,
            updated_by=tenant.user_id,
        )
        record_event(
            session,
            event_type=AuditEventType.ORG_AI_POLICY_UPDATED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type=TARGET_AI_POLICY,
            target_id=str(org_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata={
                "from_allow_external": before.policy.allow_external,
                "from_providers": sorted(before.policy.allowed_providers),
                "allow_external": stored.policy.allow_external,
                "providers": sorted(stored.policy.allowed_providers),
            },
        )
    return _out(stored, settings)
