"""``/v1/orgs/{org_id}/usage/limits`` (M11.08): org admins manage budgets; GET shows consumption.

Non-members get the concealed 404, plain members 403. Also the 429 mapping of ``QuotaExceeded``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select

from app.api.deps import FORBIDDEN, Tenant, api_error, not_found
from app.modules.organizations import service
from app.modules.usage.limits import (
    ERR_QUOTA_EXCEEDED,
    QUOTA_MESSAGE,
    QuotaExceeded,
    consumption,
    is_exceeded,
    period_bounds,
)
from app.modules.usage.models import UsageLimit

router = APIRouter(prefix="/v1/orgs", tags=["usage"])

Kind = Literal["chat", "embed", "research", "graph", "all"]


class LimitIn(BaseModel):
    scope: Literal["org", "user"] = "org"
    user_id: uuid.UUID | None = None
    kind: Kind = "all"
    period: Literal["day", "month"] = "month"
    max_tokens: int | None = Field(default=None, ge=0, le=10**13)
    max_requests: int | None = Field(default=None, ge=0, le=10**9)
    action: Literal["warn", "block"] = "block"

    @model_validator(mode="after")
    def _shape(self) -> LimitIn:
        if self.max_tokens is None and self.max_requests is None:
            raise ValueError("max_tokens or max_requests is required")
        if self.scope == "org" and self.user_id is not None:
            raise ValueError("an org limit has no user_id")
        return self


class LimitOut(LimitIn):
    id: uuid.UUID
    created_at: datetime
    consumed_tokens: int = 0
    consumed_requests: int = 0
    state: Literal["ok", "warn", "blocked"] = "ok"
    resets_at: datetime | None = None


class LimitList(BaseModel):
    limits: list[LimitOut]


def _admin(session: object, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    access = service.load_org_access(session, org_id, user_id)  # type: ignore[arg-type]
    if access is None or access.role is None:
        raise not_found()
    if access.role not in ("owner", "admin"):
        raise api_error(status.HTTP_403_FORBIDDEN, FORBIDDEN)


def _out(session: object, row: UsageLimit, now: datetime) -> LimitOut:
    start, reset = period_bounds(row.period, now)
    per_user_default = row.scope == "user" and row.user_id is None
    tokens, reqs = consumption(
        session,  # type: ignore[arg-type]
        org_id=row.org_id,
        kind=row.kind,
        start=start,
        user_id=row.user_id if row.scope == "user" else None,
        busiest_user=per_user_default,
    )
    over = is_exceeded(row.max_tokens, row.max_requests, tokens, reqs)
    state: Literal["ok", "warn", "blocked"] = (
        "ok" if not over else ("blocked" if row.action == "block" else "warn")
    )
    return LimitOut(
        id=row.id,
        created_at=row.created_at,
        scope=row.scope,  # type: ignore[arg-type]
        user_id=row.user_id,
        kind=row.kind,  # type: ignore[arg-type]
        period=row.period,  # type: ignore[arg-type]
        max_tokens=row.max_tokens,
        max_requests=row.max_requests,
        action=row.action,  # type: ignore[arg-type]
        consumed_tokens=tokens,
        consumed_requests=reqs,
        state=state,
        resets_at=reset,
    )


@router.get("/{org_id}/usage/limits", response_model=LimitList)
def list_limits(tenant: Tenant, org_id: uuid.UUID) -> LimitList:
    now = datetime.now(UTC)
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        rows = session.scalars(
            select(UsageLimit).where(UsageLimit.org_id == org_id).order_by(UsageLimit.created_at)
        ).all()
        return LimitList(limits=[_out(session, r, now) for r in rows])


@router.post("/{org_id}/usage/limits", response_model=LimitOut, status_code=201)
def create_limit(tenant: Tenant, org_id: uuid.UUID, body: LimitIn) -> LimitOut:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        row = UsageLimit(
            id=uuid.uuid4(), org_id=org_id, created_by=tenant.user_id, **body.model_dump()
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return _out(session, row, datetime.now(UTC))


@router.put("/{org_id}/usage/limits/{limit_id}", response_model=LimitOut)
def replace_limit(
    tenant: Tenant, org_id: uuid.UUID, limit_id: uuid.UUID, body: LimitIn
) -> LimitOut:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        row = session.get(UsageLimit, limit_id)
        if row is None or row.org_id != org_id:
            raise not_found()
        for key, value in body.model_dump().items():
            setattr(row, key, value)
        session.flush()
        return _out(session, row, datetime.now(UTC))


@router.delete("/{org_id}/usage/limits/{limit_id}", status_code=204)
def delete_limit(tenant: Tenant, org_id: uuid.UUID, limit_id: uuid.UUID) -> Response:
    with tenant.transaction(org_id) as session:
        _admin(session, org_id, tenant.user_id)
        done = session.execute(
            delete(UsageLimit)
            .where(UsageLimit.id == limit_id, UsageLimit.org_id == org_id)
            .returning(UsageLimit.id)
        ).first()
        if done is None:
            raise not_found()
    return Response(status_code=204)


def quota_exceeded_handler(_request: Request, exc: Exception) -> JSONResponse:
    """HTTP 429 for a model call refused by a quota: safe message, when it resets, Retry-After."""
    resets_at = exc.resets_at if isinstance(exc, QuotaExceeded) else datetime.now(UTC)
    retry = exc.retry_after_seconds() if isinstance(exc, QuotaExceeded) else 60
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": ERR_QUOTA_EXCEEDED,
            "message": QUOTA_MESSAGE,
            "resets_at": resets_at.isoformat(),
        },
        headers={"Retry-After": str(retry)},
    )
