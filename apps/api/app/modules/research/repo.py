# ruff: noqa: S608 - the SQL is built only from module constants (no caller data).
"""Data access of research sessions, steps and reports (M07.02; contract m07 section 4).

Same discipline as ``chat_repo``: API side runs in a tenant transaction (creator-private under RLS);
the worker acts for the session's creator (``worker_chat_session``) and every write it makes is
conditioned on ``lease_owner = owner AND lease_epoch = epoch AND state = 'running'`` in the SAME
statement, so a worker that lost its lease (or whose session was cancelled) matches no row and gets
:class:`SessionFenceLost`: it must stop and write nothing more. Steps are unique per
``(session, seq)``, so a resumed run can never duplicate a step.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Connection, func, insert, select, text
from sqlalchemy.orm import Session

from app.api.deps import KeysetCursor
from app.core.tracing import with_trace
from app.modules.ingestion.models import OutboxEvent
from app.modules.research.models import (
    ACTIVE_STATES,
    AGGREGATE_RESEARCH_SESSION,
    EVENT_RESEARCH_SESSION_QUEUED,
    ResearchReport,
    ResearchSession,
    ResearchStep,
)

MAX_PAGE: Final = 100


class SessionFenceLost(Exception):
    """The caller no longer holds the session (lease expired, re-claimed, cancelled, finished)."""


@dataclass(frozen=True, slots=True)
class SessionFence:
    session_id: uuid.UUID
    org_id: uuid.UUID
    owner: str
    epoch: int


@dataclass(frozen=True, slots=True)
class ClaimedSession:
    session_id: uuid.UUID
    org_id: uuid.UUID
    created_by: uuid.UUID
    attempts: int
    lease_epoch: int
    lease_owner: str

    @property
    def fence(self) -> SessionFence:
        return SessionFence(self.session_id, self.org_id, self.lease_owner, self.lease_epoch)


@dataclass(frozen=True, slots=True)
class ReapedSession:
    session_id: uuid.UUID
    org_id: uuid.UUID
    outcome: str  # 'requeued' | 'failed'


@dataclass(frozen=True, slots=True)
class StepRow:
    seq: int
    kind: str
    query: str | None
    state: str
    result: dict[str, Any]


# ------------------------------------------------------------------------------------ API side


def create_session(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    question: str,
    limits: Mapping[str, Any],
    scope_snapshot: Mapping[str, Any],
) -> uuid.UUID:
    """The session (``planned``) and its ``research.session.queued`` outbox row, atomically."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"research:{user_id}"},
    )
    session_id = uuid.uuid4()
    session.execute(
        insert(ResearchSession).values(
            id=session_id,
            org_id=org_id,
            workspace_id=workspace_id,
            created_by=user_id,
            question=question,
            limits=dict(limits),
            scope_snapshot=dict(scope_snapshot),
            usage={"model_calls": 0, "evidence_chunks": 0, "elapsed_seconds": 0.0},
        )
    )
    session.execute(
        insert(OutboxEvent).values(
            org_id=org_id,
            aggregate_type=AGGREGATE_RESEARCH_SESSION,
            aggregate_id=session_id,
            event_type=EVENT_RESEARCH_SESSION_QUEUED,
            payload=with_trace({"session_id": str(session_id)}),
        )
    )
    return session_id


def count_active(session: Session, *, user_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(ResearchSession)
            .where(ResearchSession.created_by == user_id, ResearchSession.state.in_(ACTIVE_STATES))
        ).scalar_one()
    )


def get_session(
    session: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID
) -> ResearchSession | None:
    return session.execute(
        select(ResearchSession).where(
            ResearchSession.id == session_id,
            ResearchSession.org_id == org_id,
            ResearchSession.created_by == user_id,
        )
    ).scalar_one_or_none()


def list_sessions(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int,
    after: KeysetCursor | None,
) -> tuple[list[ResearchSession], KeysetCursor | None]:
    limit = max(1, min(limit, MAX_PAGE))
    stmt = (
        select(ResearchSession)
        .where(ResearchSession.org_id == org_id, ResearchSession.created_by == user_id)
        .order_by(ResearchSession.created_at.desc(), ResearchSession.id.desc())
        .limit(limit + 1)
    )
    if after is not None:
        stmt = stmt.where(
            (ResearchSession.created_at < after.created_at)
            | ((ResearchSession.created_at == after.created_at) & (ResearchSession.id < after.id))
        )
    rows = list(session.execute(stmt).scalars())
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, KeysetCursor(created_at=rows[-1].created_at, id=rows[-1].id)
    return rows, None


def list_steps(session: Session, session_id: uuid.UUID) -> list[StepRow]:
    rows = session.execute(
        select(ResearchStep).where(ResearchStep.session_id == session_id).order_by(ResearchStep.seq)
    ).scalars()
    return [StepRow(r.seq, r.kind, r.query, r.state, dict(r.result)) for r in rows]


def get_report(session: Session, session_id: uuid.UUID) -> ResearchReport | None:
    return session.execute(
        select(ResearchReport).where(ResearchReport.session_id == session_id)
    ).scalar_one_or_none()


def cancel_session(session: Session, session_id: uuid.UUID) -> tuple[str, bool] | None:
    """Creator-only cancel (``research_cancel``). ``None`` = no such session for the caller."""
    row = session.execute(
        text("SELECT * FROM research_cancel(:s)"), {"s": session_id}
    ).one_or_none()
    if row is None:
        return None
    return str(row.state), bool(row.cancelled)


# ---------------------------------------------------------------------------------- worker side


def claim_session(
    conn: Connection, session_id: uuid.UUID, owner: str, lease_seconds: int
) -> ClaimedSession | None:
    row = (
        conn.execute(
            text("SELECT * FROM research_claim_session(:s, :o, :l)"),
            {"s": session_id, "o": owner, "l": lease_seconds},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return ClaimedSession(
        session_id=row["session_id"],
        org_id=row["org_id"],
        created_by=row["created_by"],
        attempts=row["attempts"],
        lease_epoch=row["lease_epoch"],
        lease_owner=owner,
    )


def reap_expired(conn: Connection, max_attempts: int = 3) -> list[ReapedSession]:
    rows = conn.execute(text("SELECT * FROM research_reap_expired(:m)"), {"m": max_attempts}).all()
    return [ReapedSession(r.session_id, r.org_id, r.outcome) for r in rows]


_FENCE_WHERE = (
    " r.id = :sid AND r.org_id = :org AND r.lease_owner = :owner AND r.lease_epoch = :epoch"
    " AND r.state = 'running'"
)


def _fence_params(fence: SessionFence) -> dict[str, Any]:
    return {
        "sid": fence.session_id,
        "org": fence.org_id,
        "owner": fence.owner,
        "epoch": fence.epoch,
    }


def _check(rowcount: int, fence: SessionFence) -> None:
    if rowcount != 1:
        raise SessionFenceLost(str(fence.session_id))


def renew_lease(session: Session, fence: SessionFence, lease_seconds: int) -> None:
    result = session.execute(
        text(
            "UPDATE research_sessions r SET lease_expires_at = now() + make_interval(secs => :s)"
            " WHERE" + _FENCE_WHERE
        ),
        {"s": lease_seconds, **_fence_params(fence)},
    )
    _check(result.rowcount, fence)  # type: ignore[attr-defined]


def load_for_run(session: Session, fence: SessionFence) -> tuple[ResearchSession, list[StepRow]]:
    row = session.execute(
        select(ResearchSession).where(
            ResearchSession.id == fence.session_id, ResearchSession.org_id == fence.org_id
        )
    ).scalar_one()
    return row, list_steps(session, fence.session_id)


def set_plan_and_usage(
    session: Session,
    fence: SessionFence,
    *,
    plan: Mapping[str, Any] | None,
    usage: Mapping[str, Any],
) -> None:
    result = session.execute(
        text(
            "UPDATE research_sessions r SET usage = CAST(:u AS jsonb),"
            " plan = COALESCE(CAST(:p AS jsonb), r.plan) WHERE" + _FENCE_WHERE
        ),
        {
            "u": json.dumps(dict(usage)),
            "p": None if plan is None else json.dumps(dict(plan)),
            **_fence_params(fence),
        },
    )
    _check(result.rowcount, fence)  # type: ignore[attr-defined]


def insert_step(
    session: Session, fence: SessionFence, *, seq: int, kind: str, query: str | None
) -> None:
    """Fenced; an existing ``(session, seq)`` is left alone (resume never duplicates a step)."""
    session.execute(
        text(
            "INSERT INTO research_steps (id, org_id, session_id, seq, kind, query)"
            " SELECT :id, r.org_id, r.id, :seq, :kind, :query FROM research_sessions r"
            " WHERE" + _FENCE_WHERE + " FOR SHARE OF r"
            " ON CONFLICT ON CONSTRAINT uq_research_steps_session_id_seq DO NOTHING"
        ),
        {"id": uuid.uuid4(), "seq": seq, "kind": kind, "query": query, **_fence_params(fence)},
    )
    exists = session.execute(
        text("SELECT 1 FROM research_steps WHERE session_id = :sid AND seq = :seq"),
        {"sid": fence.session_id, "seq": seq},
    ).first()
    if exists is None:
        raise SessionFenceLost(str(fence.session_id))


def update_step(
    session: Session,
    fence: SessionFence,
    *,
    seq: int,
    state: str,
    result: Mapping[str, Any] | None = None,
) -> None:
    done = state in ("done", "failed")
    res = session.execute(
        text(
            "UPDATE research_steps s SET state = :state,"
            " result = COALESCE(CAST(:res AS jsonb), s.result),"
            " started_at = COALESCE(s.started_at, now()),"
            " finished_at = CASE WHEN :done THEN now() ELSE s.finished_at END"
            " FROM research_sessions r WHERE s.session_id = r.id AND s.org_id = r.org_id"
            " AND s.seq = :seq AND" + _FENCE_WHERE
        ),
        {
            "state": state,
            "res": None if result is None else json.dumps(dict(result)),
            "done": done,
            "seq": seq,
            **_fence_params(fence),
        },
    )
    _check(res.rowcount, fence)  # type: ignore[attr-defined]


def insert_report(
    session: Session,
    fence: SessionFence,
    *,
    sections: list[Any],
    verification: Mapping[str, Any],
) -> None:
    res = session.execute(
        text(
            "INSERT INTO research_reports (session_id, org_id, sections, verification)"
            " SELECT r.id, r.org_id, CAST(:sec AS jsonb), CAST(:ver AS jsonb)"
            " FROM research_sessions r WHERE" + _FENCE_WHERE + " FOR SHARE OF r"
            " ON CONFLICT ON CONSTRAINT pk_research_reports DO NOTHING"
        ),
        {
            "sec": json.dumps(sections),
            "ver": json.dumps(dict(verification)),
            **_fence_params(fence),
        },
    )
    del res  # an existing report (a resumed finish) is kept as is


def finish_session(
    session: Session, fence: SessionFence, *, state: str, error_code: str | None = None
) -> None:
    if state not in ("completed", "failed"):
        raise ValueError(state)
    res = session.execute(
        text(
            "UPDATE research_sessions r SET state = :state, error_code = :ec, finished_at = now(),"
            " lease_owner = NULL, lease_expires_at = NULL WHERE" + _FENCE_WHERE
        ),
        {"state": state, "ec": error_code, **_fence_params(fence)},
    )
    _check(res.rowcount, fence)  # type: ignore[attr-defined]


def finished_at(session: Session, session_id: uuid.UUID) -> datetime | None:
    return session.execute(
        select(ResearchSession.finished_at).where(ResearchSession.id == session_id)
    ).scalar_one_or_none()
