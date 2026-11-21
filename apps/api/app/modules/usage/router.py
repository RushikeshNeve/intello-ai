"""``GET /v1/orgs/{org_id}/usage`` (M11.01): usage summary grouped by day, model or kind.

Org owners/admins see the whole org; any other member sees only their own events. A non-member
gets the concealed 404. Aggregates only: no prompt or answer text exists in the table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import Tenant, api_error, not_found
from app.modules.organizations import service

router = APIRouter(prefix="/v1/orgs", tags=["usage"])

GroupBy = Literal["day", "model", "kind"]
MAX_RANGE_DAYS = 366
DEFAULT_RANGE_DAYS = 30


class UsageRow(BaseModel):
    key: str
    events: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    est_cost_micros: int


class UsageSummary(BaseModel):
    group_by: GroupBy
    range_from: datetime
    range_to: datetime
    scope: Literal["org", "self"]
    totals: UsageRow
    rows: list[UsageRow]


_KEY_SQL: dict[str, str] = {
    "day": "to_char(date_trunc('day', created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD')",
    "model": "provider || '/' || model",
    "kind": "kind",
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.get(
    "/{org_id}/usage",
    response_model=UsageSummary,
    responses={404: {"description": "Organization not found or not a member (concealed)."}},
)
def get_usage(
    tenant: Tenant,
    org_id: uuid.UUID,
    group_by: Annotated[GroupBy, Query()] = "day",
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> UsageSummary:
    end = _aware(to) if to else datetime.now(UTC) + timedelta(seconds=1)
    start = _aware(from_) if from_ else end - timedelta(days=DEFAULT_RANGE_DAYS)
    if start >= end or end - start > timedelta(days=MAX_RANGE_DAYS):
        raise api_error(422, "invalid_range")
    with tenant.transaction(org_id) as session:
        access = service.load_org_access(session, org_id, tenant.user_id)
        if access is None or access.role is None:
            raise not_found()
        is_admin = access.role in ("owner", "admin")
        key_sql = _KEY_SQL[group_by]
        rows = session.execute(
            text(
                f"SELECT {key_sql} AS key, count(*) AS events,"  # noqa: S608 - fixed fragment
                " coalesce(sum(tokens_in), 0) AS tin, coalesce(sum(tokens_out), 0) AS tout,"
                " coalesce(sum(tokens_cached), 0) AS tc, coalesce(sum(est_cost_micros), 0) AS cost"
                " FROM usage_events WHERE org_id = :o AND created_at >= :s AND created_at < :e"
                " AND (:all OR user_id = :u) GROUP BY 1 ORDER BY 1"
            ),
            {"o": org_id, "s": start, "e": end, "all": is_admin, "u": tenant.user_id},
        ).all()
    out = [UsageRow(key=r[0], events=r[1], tokens_in=r[2], tokens_out=r[3], tokens_cached=r[4],
                    est_cost_micros=r[5]) for r in rows]  # fmt: skip
    totals = UsageRow(
        key="total",
        events=sum(r.events for r in out),
        tokens_in=sum(r.tokens_in for r in out),
        tokens_out=sum(r.tokens_out for r in out),
        tokens_cached=sum(r.tokens_cached for r in out),
        est_cost_micros=sum(r.est_cost_micros for r in out),
    )
    return UsageSummary(
        group_by=group_by,
        range_from=start,
        range_to=end,
        scope="org" if is_admin else "self",
        totals=totals,
        rows=out,
    )
