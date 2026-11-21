"""Budgets, quotas and burst limits (M11.08): enforcement before a model call.

``check_quota`` reads ``usage_limits`` plus an indexed aggregate over ``usage_events`` (UTC day or
month), so a hard block takes effect on the very next call (no cache). A ``block`` limit at or
over its maximum raises ``QuotaExceeded`` (HTTP 429 / run error ``quota_exceeded``); a ``warn``
limit only reports. ``QuotaGuard`` fails open: an unreadable limit table never stops a model call.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

ERR_QUOTA_EXCEEDED: Final = "quota_exceeded"
QUOTA_MESSAGE: Final = (
    "Your organization's usage limit has been reached for now. "
    "Try again after it resets, or ask an admin to raise the limit."
)


class QuotaExceeded(Exception):
    """A blocking limit (or the per-user burst limit) is reached. Carries no content, only when."""

    def __init__(self, *, kind: str, period: str, resets_at: datetime) -> None:
        super().__init__(ERR_QUOTA_EXCEEDED)
        self.kind, self.period, self.resets_at = kind, period, resets_at

    def retry_after_seconds(self, now: datetime | None = None) -> int:
        delta = self.resets_at - (now or datetime.now(UTC))
        return max(1, int(delta.total_seconds()) + 1)


@dataclass(frozen=True)
class QuotaResult:
    warnings: list[str] = field(default_factory=list)  # "<kind>/<period>" of exceeded warn limits


def period_bounds(period: str, now: datetime) -> tuple[datetime, datetime]:
    """UTC ``[start, reset)`` of the day or month containing ``now``."""
    now = now.astimezone(UTC)
    if period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def consumption(
    session: Session,
    *,
    org_id: uuid.UUID,
    kind: str,
    start: datetime,
    user_id: uuid.UUID | None,
    busiest_user: bool = False,
) -> tuple[int, int]:
    """``(tokens, requests)`` since ``start``; ``user_id`` None = whole org; ``busiest_user`` =
    the largest single user (shown for per-user defaults)."""
    params: dict[str, Any] = {"o": org_id, "s": start, "k": kind, "u": user_id}
    where = (
        "org_id = :o AND created_at >= :s AND (:k = 'all' OR kind = :k)"
        " AND (CAST(:u AS uuid) IS NULL OR user_id = CAST(:u AS uuid))"
    )
    if busiest_user:
        row = session.execute(
            text(
                "SELECT coalesce(max(t), 0), coalesce(max(r), 0) FROM ("  # noqa: S608 - fixed
                " SELECT sum(tokens_in + tokens_out) AS t, count(*) AS r FROM usage_events"
                f" WHERE {where} GROUP BY user_id) x"
            ),
            params,
        ).one()
    else:
        row = session.execute(
            text(
                "SELECT coalesce(sum(tokens_in + tokens_out), 0), count(*)"  # noqa: S608 - fixed
                f" FROM usage_events WHERE {where}"
            ),
            params,
        ).one()
    return int(row[0]), int(row[1])


def is_exceeded(max_tokens: int | None, max_requests: int | None, tokens: int, reqs: int) -> bool:
    return (max_tokens is not None and tokens >= max_tokens) or (
        max_requests is not None and reqs >= max_requests
    )


def check_quota(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    kind: str,
    now: datetime | None = None,
    burst_per_minute: int = 0,
) -> QuotaResult:
    """Raise ``QuotaExceeded`` when a blocking limit or the burst limit is reached."""
    now = now or datetime.now(UTC)
    if burst_per_minute > 0 and user_id is not None:
        _, recent = consumption(
            session, org_id=org_id, kind="all", start=now - timedelta(minutes=1), user_id=user_id
        )
        if recent >= burst_per_minute:
            raise QuotaExceeded(kind="burst", period="minute", resets_at=now + timedelta(minutes=1))
    rows = session.execute(
        text(
            "SELECT scope, user_id, kind, period, max_tokens, max_requests, action"
            " FROM usage_limits WHERE org_id = :o AND kind IN (:k, 'all')"
        ),
        {"o": org_id, "k": kind},
    ).all()
    warnings: list[str] = []
    for scope, limit_user, lkind, period, max_tokens, max_requests, action in rows:
        if scope == "user" and (user_id is None or limit_user not in (None, user_id)):
            continue
        start, reset = period_bounds(period, now)
        used_t, used_r = consumption(
            session,
            org_id=org_id,
            kind=lkind,
            start=start,
            user_id=None if scope == "org" else user_id,
        )
        if not is_exceeded(max_tokens, max_requests, used_t, used_r):
            continue
        if action == "block":
            raise QuotaExceeded(kind=lkind, period=period, resets_at=reset)
        warnings.append(f"{lkind}/{period}")
    return QuotaResult(warnings)


class QuotaGuard:
    """Binds the org/user of one unit of work; ``check(kind)`` runs just before a model call."""

    def __init__(
        self,
        sessions: Callable[[uuid.UUID], AbstractContextManager[Session]],
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> None:
        self._sessions, self._org_id, self._user_id = sessions, org_id, user_id
        self.warnings: list[str] = []

    def check(self, kind: str) -> None:
        try:
            from app.core.config import get_settings

            burst = get_settings().USAGE_USER_BURST_PER_MINUTE
            with self._sessions(self._org_id) as session:
                result = check_quota(
                    session,
                    org_id=self._org_id,
                    user_id=self._user_id,
                    kind=kind,
                    burst_per_minute=burst,
                )
            self.warnings = result.warnings
        except QuotaExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - enforcement fails open, never breaks a call
            log.warning("quota check failed type=%s", type(exc).__name__)
