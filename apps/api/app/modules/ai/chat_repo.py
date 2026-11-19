"""Data access of chat threads, messages, runs and the run event log (M04.02.04; contract m04 §8).

Everything takes a session that already carries the tenant context. API side: ``tenant_session``
(user + org). Worker side: :func:`worker_chat_session` sets BOTH the run's org and its creator,
because threads/runs are private to the creator under RLS (``chat_claim_run`` returns
``created_by`` for exactly that). Queries also filter by org/user explicitly; RLS is the backstop.

Fencing: after ``chat_claim_run`` a worker holds a :class:`RunFence` ``(run_id, org_id, owner,
epoch)``. Every write it makes to the run, its event log or its assistant message is conditioned on
``lease_owner = owner AND lease_epoch = epoch AND state = 'running'`` in the SAME statement (and the
run row is share-locked while the row is inserted, so a concurrent cancel serialises with it). A
worker that lost its lease, or whose run was cancelled, matches no row and gets
:class:`RunFenceLost`: it must stop and write nothing more.

Idempotency: ``(thread_id, idempotency_key)`` is unique; :func:`create_run` inserts the run with
``ON CONFLICT DO NOTHING`` so concurrent submissions of the same key converge on ONE run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Connection, Engine, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import KeysetCursor
from app.core.db import make_sessionmaker, set_tenant_context
from app.modules.ai.models_db import (
    ACTIVE_RUN_STATES,
    AGGREGATE_CHAT_RUN,
    EVENT_CHAT_RUN_QUEUED,
    EVENT_TYPES,
    ROLE_USER,
    RUN_STATE_COMPLETED,
    RUN_STATE_FAILED,
    ChatMessageRow,
    ChatRun,
    ChatRunEvent,
    ChatThread,
)
from app.modules.ingestion.models import OutboxEvent

DEFAULT_PAGE: Final = 50
MAX_PAGE: Final = 200
MAX_EVENTS_PER_READ: Final = 1000
_FINISH_STATES: Final = (RUN_STATE_COMPLETED, RUN_STATE_FAILED)


class ChatError(Exception):
    """Base of the repository's typed failures."""


class ThreadNotFound(ChatError):
    """No live thread of that id owned by the caller in this org (concealed 404 upstream)."""


class RunFenceLost(ChatError):
    """The caller no longer holds the run (lease expired, re-claimed, cancelled, finished)."""


class InvalidTransition(ChatError):
    """The requested state change is not allowed (only ``running -> completed|failed``)."""


class RunLimitExceeded(ChatError):
    """Per-user run limits (concurrent / per minute) are used up; ``kind`` names which."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ThreadRow:
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID | None
    created_by: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MessageRow:
    id: uuid.UUID
    thread_id: uuid.UUID
    role: str
    content: str
    citations: list[Any]
    verification: dict[str, Any] | None
    run_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunRow:
    id: uuid.UUID
    org_id: uuid.UUID
    thread_id: uuid.UUID
    user_message_id: uuid.UUID
    created_by: uuid.UUID
    state: str
    idempotency_key: str
    scope_snapshot: dict[str, Any]
    model: dict[str, Any]
    error_code: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    lease_epoch: int


@dataclass(frozen=True, slots=True)
class EventRow:
    seq: int
    run_id: uuid.UUID
    type: str
    data: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedRun:
    run_id: uuid.UUID
    message_id: uuid.UUID
    created: bool  # False = the idempotency key already had a run (a replay)


@dataclass(frozen=True, slots=True)
class CancelResult:
    run_id: uuid.UUID
    state: str
    cancelled: bool  # True = this call moved it to ``cancelled``


@dataclass(frozen=True, slots=True)
class RunFence:
    run_id: uuid.UUID
    org_id: uuid.UUID
    owner: str
    epoch: int


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    run_id: uuid.UUID
    org_id: uuid.UUID
    thread_id: uuid.UUID
    user_message_id: uuid.UUID
    created_by: uuid.UUID
    attempts: int
    lease_epoch: int
    lease_owner: str

    @property
    def fence(self) -> RunFence:
        return RunFence(self.run_id, self.org_id, self.lease_owner, self.lease_epoch)


@dataclass(frozen=True, slots=True)
class ReapedRun:
    run_id: uuid.UUID
    org_id: uuid.UUID
    outcome: str  # 'requeued' | 'failed'


def _thread(row: Any) -> ThreadRow:
    return ThreadRow(
        id=row.id,
        org_id=row.org_id,
        workspace_id=row.workspace_id,
        created_by=row.created_by,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


_THREAD_COLUMNS = (
    ChatThread.id,
    ChatThread.org_id,
    ChatThread.workspace_id,
    ChatThread.created_by,
    ChatThread.title,
    ChatThread.created_at,
    ChatThread.updated_at,
)
_RUN_COLUMNS = (
    ChatRun.id,
    ChatRun.org_id,
    ChatRun.thread_id,
    ChatRun.user_message_id,
    ChatRun.created_by,
    ChatRun.state,
    ChatRun.idempotency_key,
    ChatRun.scope_snapshot,
    ChatRun.model,
    ChatRun.error_code,
    ChatRun.attempts,
    ChatRun.created_at,
    ChatRun.started_at,
    ChatRun.finished_at,
    ChatRun.lease_epoch,
)


def _run(row: Any) -> RunRow:
    return RunRow(**{c.key: getattr(row, c.key) for c in _RUN_COLUMNS})


# --------------------------------------------------------------------------------- worker session


@contextmanager
def worker_chat_session(
    engine: Engine, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> Iterator[Session]:
    """A worker transaction acting for the run's creator (org + user); commits on success."""
    session = make_sessionmaker(engine)()
    try:
        with session.begin():
            set_tenant_context(session, user_id=user_id, org_id=org_id)
            yield session
    finally:
        session.close()


# ------------------------------------------------------------------------------------- threads


def create_thread(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
    title: str = "",
) -> ThreadRow:
    thread_id = uuid.uuid4()
    session.execute(
        insert(ChatThread).values(
            id=thread_id, org_id=org_id, workspace_id=workspace_id, created_by=user_id, title=title
        )
    )
    row = session.execute(select(*_THREAD_COLUMNS).where(ChatThread.id == thread_id)).one()
    return _thread(row)


def get_thread(
    session: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, thread_id: uuid.UUID
) -> ThreadRow | None:
    """The caller's live thread, else ``None`` (another member's thread is simply absent)."""
    row = session.execute(
        select(*_THREAD_COLUMNS).where(
            ChatThread.id == thread_id,
            ChatThread.org_id == org_id,
            ChatThread.created_by == user_id,
            ChatThread.deleted_at.is_(None),
        )
    ).one_or_none()
    return None if row is None else _thread(row)


def list_threads(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = DEFAULT_PAGE,
    after: KeysetCursor | None = None,
) -> tuple[list[ThreadRow], KeysetCursor | None]:
    """The caller's live threads, newest first, keyset on ``(created_at, id)``."""
    limit = max(1, min(limit, MAX_PAGE))
    stmt = (
        select(*_THREAD_COLUMNS)
        .where(
            ChatThread.org_id == org_id,
            ChatThread.created_by == user_id,
            ChatThread.deleted_at.is_(None),
        )
        .order_by(ChatThread.created_at.desc(), ChatThread.id.desc())
        .limit(limit + 1)
    )
    if after is not None:
        stmt = stmt.where(
            (ChatThread.created_at < after.created_at)
            | ((ChatThread.created_at == after.created_at) & (ChatThread.id < after.id))
        )
    rows = [_thread(r) for r in session.execute(stmt).all()]
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        return rows, KeysetCursor(created_at=last.created_at, id=last.id)
    return rows, None


def set_thread_title(
    session: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, thread_id: uuid.UUID, title: str
) -> bool:
    result = session.execute(
        update(ChatThread)
        .where(
            ChatThread.id == thread_id,
            ChatThread.org_id == org_id,
            ChatThread.created_by == user_id,
            ChatThread.deleted_at.is_(None),
        )
        .values(title=title, updated_at=func.now())
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def soft_delete_thread(
    session: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, thread_id: uuid.UUID
) -> bool:
    """Soft-delete the caller's thread and cancel its active runs (so no worker keeps streaming into
    a deleted thread). ``False`` = no such live thread."""
    result = session.execute(
        update(ChatThread)
        .where(
            ChatThread.id == thread_id,
            ChatThread.org_id == org_id,
            ChatThread.created_by == user_id,
            ChatThread.deleted_at.is_(None),
        )
        .values(deleted_at=func.now(), updated_at=func.now())
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        return False
    active = session.execute(
        select(ChatRun.id).where(
            ChatRun.thread_id == thread_id,
            ChatRun.created_by == user_id,
            ChatRun.state.in_(ACTIVE_RUN_STATES),
        )
    ).scalars()
    for run_id in list(active):
        cancel_run(session, run_id)
    return True


# ------------------------------------------------------------------------------------ messages


def list_messages(
    session: Session, *, org_id: uuid.UUID, thread_id: uuid.UUID, limit: int = 500
) -> list[MessageRow]:
    """Oldest first. Under RLS only the creator's thread yields rows."""
    rows = session.execute(
        select(ChatMessageRow)
        .where(ChatMessageRow.thread_id == thread_id, ChatMessageRow.org_id == org_id)
        .order_by(ChatMessageRow.created_at, ChatMessageRow.id)
        .limit(max(1, min(limit, 2000)))
    ).scalars()
    return [
        MessageRow(
            id=m.id,
            thread_id=m.thread_id,
            role=m.role,
            content=m.content,
            citations=list(m.citations),
            verification=m.verification,
            run_id=m.run_id,
            created_at=m.created_at,
        )
        for m in rows
    ]


def append_assistant_message(
    session: Session,
    fence: RunFence,
    *,
    thread_id: uuid.UUID,
    content: str,
    citations: Sequence[Mapping[str, Any]] = (),
    verification: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    """The run's answer (kept claims, citation refs, verification), fenced like an event write."""
    message_id = uuid.uuid4()
    result = session.execute(
        text(
            "INSERT INTO chat_messages (id, org_id, thread_id, role, content, citations,"
            " verification, run_id)"
            " SELECT :id, r.org_id, r.thread_id, 'assistant', :content,"
            " CAST(:citations AS jsonb), CAST(:verification AS jsonb), r.id"
            " FROM chat_runs r WHERE r.id = :run AND r.org_id = :org AND r.thread_id = :thread"
            " AND r.lease_owner = :owner AND r.lease_epoch = :epoch AND r.state = 'running'"
            " FOR SHARE OF r"
        ),
        {
            "id": message_id,
            "content": content,
            "citations": json.dumps(list(citations)),
            "verification": None if verification is None else json.dumps(dict(verification)),
            "run": fence.run_id,
            "org": fence.org_id,
            "thread": thread_id,
            "owner": fence.owner,
            "epoch": fence.epoch,
        },
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RunFenceLost(str(fence.run_id))
    session.execute(
        text("UPDATE chat_threads SET updated_at = now() WHERE id = :t AND org_id = :o"),
        {"t": thread_id, "o": fence.org_id},
    )
    return message_id


# --------------------------------------------------------------------------------------- runs


def create_run(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    content: str,
    idempotency_key: str,
    scope_snapshot: Mapping[str, Any],
    model: Mapping[str, Any] | None = None,
    max_concurrent: int | None = None,
    max_per_minute: int | None = None,
) -> CreatedRun:
    """Insert the run, its user message and the ``chat.run.queued`` outbox row atomically.

    The same ``(thread, idempotency_key)`` returns the same run (``created=False``), also under
    concurrency (``ON CONFLICT DO NOTHING`` waits for an in-flight twin to commit). Limits, when
    given, are counted per user under an advisory lock so concurrent submissions cannot both slip
    through; a replay is answered before limits are applied. Raises :class:`ThreadNotFound` (not
    the caller's, deleted, other org) or :class:`RunLimitExceeded`.
    """
    if get_thread(session, org_id=org_id, user_id=user_id, thread_id=thread_id) is None:
        raise ThreadNotFound(str(thread_id))
    limited = max_concurrent is not None or max_per_minute is not None
    if limited:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"chat_runs:{user_id}"},
        )
        existing = _existing_run(session, thread_id, idempotency_key)
        if existing is not None:
            return CreatedRun(existing.id, existing.user_message_id, created=False)
        if (
            max_concurrent is not None
            and count_active_runs(session, user_id=user_id) >= max_concurrent
        ):
            raise RunLimitExceeded("concurrent")
        if (
            max_per_minute is not None
            and count_runs_since(session, user_id=user_id, seconds=60) >= max_per_minute
        ):
            raise RunLimitExceeded("per_minute")

    run_id, message_id = uuid.uuid4(), uuid.uuid4()
    inserted = session.execute(
        pg_insert(ChatRun)
        .values(
            id=run_id,
            org_id=org_id,
            thread_id=thread_id,
            user_message_id=message_id,
            created_by=user_id,
            idempotency_key=idempotency_key,
            scope_snapshot=dict(scope_snapshot),
            model=dict(model or {}),
        )
        .on_conflict_do_nothing(constraint="uq_chat_runs_thread_id_idempotency_key")
        .returning(ChatRun.id)
    ).scalar_one_or_none()
    if inserted is None:
        existing = _existing_run(session, thread_id, idempotency_key)
        if existing is None:  # pragma: no cover - the winner rolled back between the two statements
            raise ChatError("idempotency conflict without a surviving run")
        return CreatedRun(existing.id, existing.user_message_id, created=False)

    session.execute(
        insert(ChatMessageRow).values(
            id=message_id,
            org_id=org_id,
            thread_id=thread_id,
            role=ROLE_USER,
            content=content,
            run_id=run_id,
        )
    )
    session.execute(
        insert(OutboxEvent).values(
            org_id=org_id,
            aggregate_type=AGGREGATE_CHAT_RUN,
            aggregate_id=run_id,
            event_type=EVENT_CHAT_RUN_QUEUED,
            payload={"run_id": str(run_id)},
        )
    )
    session.execute(
        update(ChatThread).where(ChatThread.id == thread_id).values(updated_at=func.now())
    )
    return CreatedRun(run_id, message_id, created=True)


def _existing_run(session: Session, thread_id: uuid.UUID, key: str) -> RunRow | None:
    row = session.execute(
        select(*_RUN_COLUMNS).where(ChatRun.thread_id == thread_id, ChatRun.idempotency_key == key)
    ).one_or_none()
    return None if row is None else _run(row)


def get_run(
    session: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, run_id: uuid.UUID
) -> RunRow | None:
    """The caller's own run, else ``None``."""
    row = session.execute(
        select(*_RUN_COLUMNS).where(
            ChatRun.id == run_id, ChatRun.org_id == org_id, ChatRun.created_by == user_id
        )
    ).one_or_none()
    return None if row is None else _run(row)


def cancel_run(session: Session, run_id: uuid.UUID) -> CancelResult | None:
    """Creator-only cancel through ``chat_cancel_run`` (uses the session's user/org context).
    ``None`` = no such run for the caller. Idempotent: a finished run is returned unchanged."""
    row = session.execute(text("SELECT * FROM chat_cancel_run(:r)"), {"r": run_id}).one_or_none()
    if row is None:
        return None
    return CancelResult(run_id=row.run_id, state=row.state, cancelled=bool(row.cancelled))


def count_active_runs(session: Session, *, user_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(ChatRun)
            .where(ChatRun.created_by == user_id, ChatRun.state.in_(ACTIVE_RUN_STATES))
        ).scalar_one()
    )


def count_runs_since(session: Session, *, user_id: uuid.UUID, seconds: int) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(ChatRun)
            .where(
                ChatRun.created_by == user_id,
                ChatRun.created_at
                >= text("now() - make_interval(secs => :s)").bindparams(s=seconds),
            )
        ).scalar_one()
    )


# ------------------------------------------------------------------------------ worker: claim


def claim_run(
    conn: Connection, run_id: uuid.UUID, owner: str, lease_seconds: int
) -> ClaimedRun | None:
    """``chat_claim_run`` on a no-org connection (``system_connection``). ``None`` = nothing to do
    (duplicate delivery, already running or finished)."""
    row = (
        conn.execute(
            text("SELECT * FROM chat_claim_run(:r, :o, :l)"),
            {"r": run_id, "o": owner, "l": lease_seconds},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return ClaimedRun(
        run_id=row["run_id"],
        org_id=row["org_id"],
        thread_id=row["thread_id"],
        user_message_id=row["user_message_id"],
        created_by=row["created_by"],
        attempts=row["attempts"],
        lease_epoch=row["lease_epoch"],
        lease_owner=owner,
    )


def reap_expired_runs(conn: Connection, max_attempts: int = 2) -> list[ReapedRun]:
    rows = conn.execute(text("SELECT * FROM chat_reap_expired_runs(:m)"), {"m": max_attempts}).all()
    return [ReapedRun(r.run_id, r.org_id, r.outcome) for r in rows]


def readable_workspaces(
    conn: Connection, org_id: uuid.UUID, user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """``chat_readable_workspaces``: workspaces the user may read now (worker scope re-resolve)."""
    rows = conn.execute(
        text("SELECT workspace_id FROM chat_readable_workspaces(:o, :u)"),
        {"o": org_id, "u": user_id},
    ).scalars()
    return frozenset(rows)


# ------------------------------------------------------------------------ worker: fenced writes


def renew_lease(session: Session, fence: RunFence, lease_seconds: int) -> None:
    result = session.execute(
        text(
            "UPDATE chat_runs SET lease_expires_at = now() + make_interval(secs => :s)"
            " WHERE id = :run AND org_id = :org AND lease_owner = :owner AND lease_epoch = :epoch"
            " AND state = 'running'"
        ),
        {
            "s": lease_seconds,
            "run": fence.run_id,
            "org": fence.org_id,
            "owner": fence.owner,
            "epoch": fence.epoch,
        },
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RunFenceLost(str(fence.run_id))


def set_run_model(session: Session, fence: RunFence, model: Mapping[str, Any]) -> None:
    result = session.execute(
        text(
            "UPDATE chat_runs SET model = CAST(:m AS jsonb)"
            " WHERE id = :run AND org_id = :org AND lease_owner = :owner AND lease_epoch = :epoch"
            " AND state = 'running'"
        ),
        {
            "m": json.dumps(dict(model)),
            "run": fence.run_id,
            "org": fence.org_id,
            "owner": fence.owner,
            "epoch": fence.epoch,
        },
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RunFenceLost(str(fence.run_id))


def append_event(
    session: Session, fence: RunFence, *, type: str, data: Mapping[str, Any] | None = None
) -> int:
    """Append one event to the run's log, fenced; returns its ``seq``. Raises
    :class:`RunFenceLost` if the lease/epoch/state no longer match (nothing is written)."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown run event type: {type!r}")
    seq = session.execute(
        text(
            "INSERT INTO chat_run_events (run_id, org_id, type, data)"
            " SELECT r.id, r.org_id, :type, CAST(:data AS jsonb) FROM chat_runs r"
            " WHERE r.id = :run AND r.org_id = :org AND r.lease_owner = :owner"
            " AND r.lease_epoch = :epoch AND r.state = 'running' FOR SHARE OF r"
            " RETURNING seq"
        ),
        {
            "type": type,
            "data": json.dumps(dict(data or {})),
            "run": fence.run_id,
            "org": fence.org_id,
            "owner": fence.owner,
            "epoch": fence.epoch,
        },
    ).scalar_one_or_none()
    if seq is None:
        raise RunFenceLost(str(fence.run_id))
    return int(seq)


def finish_run(
    session: Session, fence: RunFence, *, state: str, error_code: str | None = None
) -> None:
    """Fenced ``running -> completed | failed`` (clears the lease, sets ``finished_at``)."""
    if state not in _FINISH_STATES:
        raise InvalidTransition(state)
    if state == RUN_STATE_COMPLETED and error_code is not None:
        raise InvalidTransition("a completed run has no error code")
    result = session.execute(
        text(
            "UPDATE chat_runs SET state = :state, error_code = :ec, finished_at = now(),"
            " lease_owner = NULL, lease_expires_at = NULL"
            " WHERE id = :run AND org_id = :org AND lease_owner = :owner AND lease_epoch = :epoch"
            " AND state = 'running'"
        ),
        {
            "state": state,
            "ec": error_code,
            "run": fence.run_id,
            "org": fence.org_id,
            "owner": fence.owner,
            "epoch": fence.epoch,
        },
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RunFenceLost(str(fence.run_id))


# ----------------------------------------------------------------------------- reading events


def read_events_after(
    session: Session, *, org_id: uuid.UUID, run_id: uuid.UUID, after_seq: int = 0, limit: int = 500
) -> list[EventRow]:
    """Events of one run with ``seq > after_seq`` in order (SSE replay). Under RLS only the run
    creator's events are visible."""
    rows = session.execute(
        select(ChatRunEvent)
        .where(
            ChatRunEvent.run_id == run_id,
            ChatRunEvent.org_id == org_id,
            ChatRunEvent.seq > after_seq,
        )
        .order_by(ChatRunEvent.seq)
        .limit(max(1, min(limit, MAX_EVENTS_PER_READ)))
    ).scalars()
    return [EventRow(e.seq, e.run_id, e.type, dict(e.data), e.created_at) for e in rows]


__all__ = [
    "CancelResult",
    "ChatError",
    "ClaimedRun",
    "CreatedRun",
    "EventRow",
    "InvalidTransition",
    "MessageRow",
    "ReapedRun",
    "RunFence",
    "RunFenceLost",
    "RunLimitExceeded",
    "RunRow",
    "ThreadNotFound",
    "ThreadRow",
    "append_assistant_message",
    "append_event",
    "cancel_run",
    "claim_run",
    "count_active_runs",
    "count_runs_since",
    "create_run",
    "create_thread",
    "finish_run",
    "get_run",
    "get_thread",
    "list_messages",
    "list_threads",
    "read_events_after",
    "readable_workspaces",
    "reap_expired_runs",
    "renew_lease",
    "set_run_model",
    "set_thread_title",
    "soft_delete_thread",
    "worker_chat_session",
]
