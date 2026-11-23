"""Abuse protection and canary documents (M14.07, contract: m14-contracts.md "M14.07").

* :func:`guard` counts a user's search / chat / evidence-read / export calls and the distinct
  documents they read in a sliding window (Redis sorted sets, in-process fallback). Crossing a
  per-org threshold records ONE ``abuse_anomaly`` security event per window and, by the org's
  action, flags only, throttles (429 ``abuse_throttled`` while over the threshold) or blocks the
  user temporarily (``block_seconds``). Counters fail open: an infrastructure error never denies.
* :func:`canary_check` raises a high-severity ``canary_hit`` for any retrieved/read canary
  document. Events carry ids and counts only, never a query, answer or document text.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Final, Protocol

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit import AuditEventType, record_event
from app.modules.firewall.abuse_models import AbuseSettings, CanaryDocument, SecurityEvent

log = logging.getLogger(__name__)

ABUSE_THROTTLED: Final = "abuse_throttled"
KINDS: Final = ("search", "chat", "evidence_read", "export")
CANARY_DEDUPE_SECONDS: Final = 60
_SETTINGS_TTL: Final = 20.0


class SessionOpener(Protocol):
    def __call__(
        self, *, user_id: uuid.UUID | None, org_id: uuid.UUID | None
    ) -> AbstractContextManager[Session]: ...


@dataclass(frozen=True)
class Thresholds:
    action: str = "flag"
    window_seconds: int = 300
    block_seconds: int = 900
    max_search: int = 120
    max_chat: int = 60
    max_evidence_reads: int = 200
    max_export: int = 20
    max_distinct_docs: int = 150

    def limit(self, kind: str) -> int:
        return {
            "search": self.max_search,
            "chat": self.max_chat,
            "evidence_read": self.max_evidence_reads,
            "export": self.max_export,
        }[kind]


DEFAULTS: Final = Thresholds()


# --- sliding-window counters -----------------------------------------------------------------


class Counters:
    """``hit`` adds a member to a window and returns the number of distinct members in it."""

    def __init__(self, redis_url: str | None, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._url = redis_url
        self._lock = threading.Lock()
        self._mem: dict[str, deque[tuple[float, str]]] = {}
        self._until: dict[str, float] = {}  # block key -> expiry (memory fallback)
        self._flagged: dict[str, float] = {}  # flag-once gate (memory)
        self._r: Any = None
        self._down_until = 0.0

    def _redis(self) -> Any:
        now = self._clock()
        if not self._url or now < self._down_until:
            return None
        if self._r is None:
            try:
                import redis

                self._r = redis.Redis.from_url(
                    self._url, socket_timeout=0.3, socket_connect_timeout=0.3
                )
            except Exception:  # noqa: BLE001
                self._down_until = now + 30
                return None
        return self._r

    def _fail(self) -> None:
        self._down_until = self._clock() + 30

    def hit(self, key: str, member: str, window: int) -> int:
        now = self._clock()
        r = self._redis()
        if r is not None:
            try:
                pipe = r.pipeline()
                pipe.zremrangebyscore(key, 0, now - window)
                pipe.zadd(key, {member: now})
                pipe.zcard(key)
                pipe.expire(key, window + 5)
                return int(pipe.execute()[2])
            except Exception:  # noqa: BLE001 - fall back to the in-process window
                self._fail()
        with self._lock:
            q = self._mem.setdefault(key, deque())
            while q and q[0][0] <= now - window:
                q.popleft()
            q.append((now, member))
            if len(self._mem) > 50_000:
                self._mem = {k: v for k, v in self._mem.items() if v and v[-1][0] > now - window}
            return len({m for _, m in q})

    def set_until(self, key: str, seconds: int) -> None:
        r = self._redis()
        if r is not None:
            try:
                r.set(key, "1", ex=seconds)
                return
            except Exception:  # noqa: BLE001
                self._fail()
        with self._lock:
            self._until[key] = self._clock() + seconds

    def remaining(self, key: str) -> int:
        r = self._redis()
        if r is not None:
            try:
                ttl = int(r.ttl(key))
                return max(ttl, 0)
            except Exception:  # noqa: BLE001
                self._fail()
        with self._lock:
            left = self._until.get(key, 0.0) - self._clock()
            return max(0, int(left + 0.999)) if left > 0 else 0

    def first_in_window(self, key: str, seconds: int) -> bool:
        """True once per ``seconds`` for ``key`` (the cheap gate before the DB dedupe)."""
        r = self._redis()
        if r is not None:
            try:
                return bool(r.set(key, "1", ex=seconds, nx=True))
            except Exception:  # noqa: BLE001
                self._fail()
        with self._lock:
            now = self._clock()
            if self._flagged.get(key, 0.0) > now:
                return False
            self._flagged[key] = now + seconds
            return True


_counters: Counters | None = None
_settings_cache: dict[uuid.UUID, tuple[float, Thresholds]] = {}


def counters() -> Counters:
    global _counters
    if _counters is None:
        url: str | None
        try:
            from app.core.config import get_settings

            url = get_settings().REDIS_URL.get_secret_value()
        except Exception:  # noqa: BLE001
            url = None
        _counters = Counters(url)
    return _counters


def set_counters(value: Counters | None) -> None:
    """Test hook: replace the process-wide counters (``None`` resets to lazy default)."""
    global _counters
    _counters = value
    _settings_cache.clear()


def invalidate_settings(org_id: uuid.UUID) -> None:
    _settings_cache.pop(org_id, None)


# --- settings --------------------------------------------------------------------------------


def to_thresholds(row: AbuseSettings | None) -> Thresholds:
    if row is None:
        return DEFAULTS
    return Thresholds(
        action=row.action,
        window_seconds=row.window_seconds,
        block_seconds=row.block_seconds,
        max_search=row.max_search,
        max_chat=row.max_chat,
        max_evidence_reads=row.max_evidence_reads,
        max_export=row.max_export,
        max_distinct_docs=row.max_distinct_docs,
    )


def load_thresholds(session: Session, org_id: uuid.UUID) -> Thresholds:
    return to_thresholds(session.get(AbuseSettings, org_id))


def _thresholds(sessions: SessionOpener, org_id: uuid.UUID, user_id: uuid.UUID) -> Thresholds:
    now = time.monotonic()
    cached = _settings_cache.get(org_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    with sessions(user_id=user_id, org_id=org_id) as session:
        value = load_thresholds(session, org_id)
    _settings_cache[org_id] = (now + _SETTINGS_TTL, value)
    return value


# --- guard -----------------------------------------------------------------------------------


def _throttled(retry_after: int) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        ABUSE_THROTTLED,
        headers={"Retry-After": str(max(1, retry_after))},
    )


def guard(
    sessions: SessionOpener,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str | None,
    *,
    document_ids: Iterable[uuid.UUID] = (),
    request_id: str | None = None,
) -> None:
    """Count one call; flag/throttle/block by the org's action. Raises 429 ``abuse_throttled``.

    Infrastructure failures (Redis, database) are swallowed: abuse protection fails open.
    """
    try:
        _guard(sessions, org_id, user_id, kind, tuple(document_ids), request_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("abuse guard skipped (%s)", type(exc).__name__)


def _guard(
    sessions: SessionOpener,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str | None,
    document_ids: tuple[uuid.UUID, ...],
    request_id: str | None,
) -> None:
    cfg = _thresholds(sessions, org_id, user_id)
    if cfg.action == "off":
        return
    c = counters()
    base = f"ukos:abuse:{org_id}:{user_id}"
    wait = c.remaining(f"{base}:block")
    if wait > 0:
        raise _throttled(wait)
    window = cfg.window_seconds
    counts: dict[str, int] = {}
    if kind is not None:
        counts[kind] = c.hit(f"{base}:{kind}", uuid.uuid4().hex, window)
    distinct = 0
    if document_ids:
        for doc in document_ids:
            distinct = c.hit(f"{base}:docs", str(doc), window)
        counts["distinct_documents"] = distinct
    over = [
        k
        for k, v in counts.items()
        if v > (cfg.max_distinct_docs if k == "distinct_documents" else cfg.limit(k))
    ]
    if not over:
        return
    if c.first_in_window(f"{base}:flag", window):
        _record_anomaly(sessions, org_id, user_id, counts, over, cfg, request_id)
    if cfg.action == "block":
        c.set_until(f"{base}:block", cfg.block_seconds)
        raise _throttled(cfg.block_seconds)
    if cfg.action == "throttle":
        raise _throttled(window)


def _record_anomaly(
    sessions: SessionOpener,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    counts: dict[str, int],
    over: list[str],
    cfg: Thresholds,
    request_id: str | None,
) -> None:
    with sessions(user_id=user_id, org_id=org_id) as session:
        recent = session.scalar(
            select(func.count())
            .select_from(SecurityEvent)
            .where(
                SecurityEvent.org_id == org_id,
                SecurityEvent.user_id == user_id,
                SecurityEvent.kind == "abuse_anomaly",
                SecurityEvent.created_at
                > func.now() - func.make_interval(0, 0, 0, 0, 0, 0, cfg.window_seconds),
            )
        )
        if recent:
            return  # another process already flagged this window
        event_id = uuid.uuid4()
        payload: dict[str, Any] = {
            **counts,
            "exceeded": over,
            "window_seconds": cfg.window_seconds,
            "action": cfg.action,
        }
        session.add(
            SecurityEvent(
                id=event_id,
                org_id=org_id,
                user_id=user_id,
                kind="abuse_anomaly",
                severity="medium" if cfg.action in ("flag", "off") else "high",
                counts=payload,
            )
        )
        session.flush()
        record_event(
            session,
            event_type=AuditEventType.SECURITY_ABUSE_FLAGGED,
            actor_user_id=user_id,
            org_id=org_id,
            target_type="security_event",
            target_id=str(event_id),
            outcome="success",
            request_id=request_id,
            metadata=payload,
        )


# --- canaries --------------------------------------------------------------------------------


def canary_document_ids(session: Session, org_id: uuid.UUID) -> set[uuid.UUID]:
    """Canary ids of the org: eval and analytics queries must exclude these."""
    return set(
        session.scalars(select(CanaryDocument.document_id).where(CanaryDocument.org_id == org_id))
    )


def canary_check(
    sessions: SessionOpener,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    document_ids: Iterable[uuid.UUID],
    *,
    run_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> int:
    """Raise a high-severity ``canary_hit`` per canary document in ``document_ids``.

    A canary's intended audience is nobody: any retrieval, read or citation is an alert. The same
    (user, document) pair alerts once per ``CANARY_DEDUPE_SECONDS``. Fails open; returns the number
    of events written. Ids and counts only, never text.
    """
    ids = set(document_ids)
    if not ids:
        return 0
    try:
        written = 0
        with sessions(user_id=user_id, org_id=org_id) as session:
            hits = set(
                session.scalars(
                    select(CanaryDocument.document_id).where(
                        CanaryDocument.org_id == org_id, CanaryDocument.document_id.in_(ids)
                    )
                )
            )
            for doc in sorted(hits, key=str):
                cond = [
                    SecurityEvent.org_id == org_id,
                    SecurityEvent.kind == "canary_hit",
                    SecurityEvent.document_id == doc,
                    SecurityEvent.created_at
                    > func.now() - func.make_interval(0, 0, 0, 0, 0, 0, CANARY_DEDUPE_SECONDS),
                ]
                cond.append(
                    SecurityEvent.user_id == user_id
                    if user_id is not None
                    else SecurityEvent.user_id.is_(None)
                )
                if session.scalar(select(func.count()).select_from(SecurityEvent).where(*cond)):
                    continue
                event_id = uuid.uuid4()
                session.add(
                    SecurityEvent(
                        id=event_id,
                        org_id=org_id,
                        user_id=user_id,
                        kind="canary_hit",
                        severity="high",
                        counts={"canary_documents": 1},
                        run_id=run_id,
                        document_id=doc,
                    )
                )
                session.flush()
                record_event(
                    session,
                    event_type=AuditEventType.SECURITY_CANARY_HIT,
                    actor_user_id=user_id,
                    org_id=org_id,
                    target_type="security_event",
                    target_id=str(event_id),
                    outcome="success",
                    request_id=request_id,
                    metadata={
                        "document_id": str(doc),
                        "run_id": str(run_id) if run_id else None,
                    },
                )
                written += 1
        return written
    except Exception as exc:  # noqa: BLE001
        log.warning("canary check skipped (%s)", type(exc).__name__)
        return 0
