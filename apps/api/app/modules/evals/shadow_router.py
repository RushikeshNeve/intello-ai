"""Prompt shadow admin API (M12.09), org owners/admins only (``org.update``).

* ``GET    /v1/orgs/{o}/prompt-shadow``                     the configured candidate + enabled flag
* ``PUT    /v1/orgs/{o}/prompt-shadow/{kind}``              mark an own DRAFT version, rate 0-20 %
* ``DELETE /v1/orgs/{o}/prompt-shadow/{kind}``              stop shadowing
* ``GET    /v1/orgs/{o}/prompt-shadow/runs?candidate_id=``  comparison rows (ids and numbers)
* ``GET    /v1/orgs/{o}/prompt-shadow/results``             per candidate: win/tie/loss, mean deltas

Another org's version is a concealed 404. Nothing here (or in storage) holds question/answer text.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, api_error, not_found
from app.core.config import Settings, get_settings
from app.modules.authorization.policy import Action
from app.modules.organizations import service as org_service
from app.modules.prompts import service as prompt_service
from app.modules.prompts import shadow

router = APIRouter(prefix="/v1/orgs/{org_id}/prompt-shadow", tags=["prompts"])

TARGET = "prompt_shadow"
ShadowKind = Literal["chat_system"]


class CandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: uuid.UUID
    rate_pct: int = Field(default=0, ge=0, le=shadow.MAX_RATE_PCT)


class CandidateOut(BaseModel):
    kind: str
    version_id: uuid.UUID
    version: int
    rate_pct: int
    updated_at: datetime


class ShadowConfigOut(BaseModel):
    enabled: bool
    candidate: CandidateOut | None


class ShadowRunOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    candidate_version_id: uuid.UUID
    status: str
    skip_reason: str | None
    live_status: str | None
    live_citation_validity: float | None
    live_latency_ms: int | None
    cand_status: str | None
    cand_citation_validity: float | None
    cand_latency_ms: int | None
    tokens_in: int | None
    tokens_out: int | None
    outcome: str | None
    created_at: datetime
    finished_at: datetime | None


class ShadowRunList(BaseModel):
    items: list[ShadowRunOut]


class ResultOut(BaseModel):
    candidate_version_id: uuid.UUID
    version: int
    state: str
    total: int
    completed: int
    skipped: int
    failed: int
    wins: int
    ties: int
    losses: int
    latency_delta_ms: float | None
    mean_tokens_in: float | None
    mean_tokens_out: float | None


class ResultList(BaseModel):
    items: list[ResultOut]


def _access(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> None:
    access = org_service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    access.require(Action.ORG_UPDATE, target_type=TARGET)


def _candidate_out(row: dict[str, Any] | None) -> CandidateOut | None:
    if row is None:
        return None
    return CandidateOut(
        kind=row["kind"],
        version_id=row["version_id"],
        version=row["version"],
        rate_pct=row["rate_pct"],
        updated_at=row["updated_at"],
    )


@router.get("", response_model=ShadowConfigOut)
def get_config(
    tenant: Tenant, org_id: uuid.UUID, settings: Annotated[Settings, Depends(get_settings)]
) -> ShadowConfigOut:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        row = shadow.get_candidate(session, org_id)
    return ShadowConfigOut(enabled=settings.PROMPT_SHADOW_ENABLED, candidate=_candidate_out(row))


@router.put("/{kind}", response_model=ShadowConfigOut)
def set_candidate(
    tenant: Tenant,
    org_id: uuid.UUID,
    kind: ShadowKind,
    body: CandidateIn,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ShadowConfigOut:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        version = prompt_service.get_own(session, org_id, body.version_id)
        if version is None or version["kind"] != kind:
            raise not_found()
        if version["state"] != "draft":
            raise api_error(status.HTTP_409_CONFLICT, "not_a_draft")
        shadow.set_candidate(session, org_id, tenant.user_id, body.version_id, body.rate_pct, kind)
        row = shadow.get_candidate(session, org_id, kind)
    return ShadowConfigOut(enabled=settings.PROMPT_SHADOW_ENABLED, candidate=_candidate_out(row))


@router.delete("/{kind}", status_code=status.HTTP_204_NO_CONTENT)
def clear_candidate(tenant: Tenant, org_id: uuid.UUID, kind: ShadowKind) -> None:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        if not shadow.clear_candidate(session, org_id, kind):
            raise not_found()


@router.get("/runs", response_model=ShadowRunList)
def list_runs(
    tenant: Tenant,
    org_id: uuid.UUID,
    candidate_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ShadowRunList:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        rows = shadow.list_runs(session, org_id, candidate_id, limit)
    return ShadowRunList(items=[ShadowRunOut(**r) for r in rows])


@router.get("/results", response_model=ResultList)
def results(tenant: Tenant, org_id: uuid.UUID) -> ResultList:
    with tenant.transaction(org_id) as session:
        _access(tenant, session, org_id)
        rows = shadow.aggregate(session, org_id)
    return ResultList(items=[ResultOut(**r) for r in rows])
