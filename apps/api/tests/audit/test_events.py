"""Unit tests for audit event validation and statement shape (no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast, get_args

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.models import AUDIT_OUTCOMES
from app.modules.audit import AuditEventType, EventCursor, Outcome, record_event
from app.modules.audit.events import build_insert
from app.modules.audit.redaction import REDACTED

PG = postgresql.dialect()  # type: ignore[no-untyped-call]

CONTRACT_EVENT_TYPES = {
    "org.created",
    "org.updated",
    "org.deleted",
    "org.member.added",
    "org.member.role_changed",
    "org.member.removed",
    "workspace.created",
    "workspace.updated",
    "workspace.deleted",
    "workspace.member.added",
    "workspace.member.role_changed",
    "workspace.member.removed",
    "grant.created",
    "grant.revoked",
    "authz.denied",
    "user.provisioned",
    "document.created",
    "document.version.created",
    "document.updated",
    "document.deleted",
    "document.version.indexed",
    "document.ingestion.retried",
    "search.performed",
    "org.ai_policy.updated",
    "chat.run.created",
    "chat.thread.deleted",
    "graph.merge.decided",
    "source.created",
    "source.sync.requested",
    "source.revoked",
    "memory.suggested",
    "memory.approved",
    "memory.rejected",
    "memory.deleted",
    "research.created",
    "research.cancelled",
}


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> None:
        self.statements.append(stmt)


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_type": "org.member.added",
        "actor_user_id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "target_type": "user",
        "target_id": str(uuid.uuid4()),
        "outcome": "success",
        "request_id": "req-1",
        "metadata": {"role": "member", "client_secret": "s"},
    }
    base.update(overrides)
    return base


def _params(**overrides: Any) -> dict[str, Any]:
    compiled = build_insert(**_kwargs(**overrides)).compile(dialect=PG)
    return dict(compiled.params)


def test_event_types_match_contract() -> None:
    assert {t.value for t in AuditEventType} == CONTRACT_EVENT_TYPES


def test_outcome_literal_matches_schema() -> None:
    assert set(get_args(Outcome)) == set(AUDIT_OUTCOMES)


@pytest.mark.parametrize("bad", ["org.exploded", "", "ORG.CREATED", "authz.denied "])
def test_unknown_event_type_rejected(bad: str) -> None:
    session = RecordingSession()
    with pytest.raises(ValueError, match="unknown audit event type"):
        record_event(cast(Session, session), **_kwargs(event_type=bad))
    assert session.statements == []


def test_invalid_outcome_rejected() -> None:
    with pytest.raises(ValueError, match="invalid audit outcome"):
        build_insert(**_kwargs(outcome="ok"))


def test_empty_target_type_rejected() -> None:
    with pytest.raises(ValueError, match="target_type"):
        build_insert(**_kwargs(target_type=""))


def test_enum_member_accepted() -> None:
    session = RecordingSession()
    record_event(cast(Session, session), **_kwargs(event_type=AuditEventType.ORG_MEMBER_REMOVED))
    assert len(session.statements) == 1


def test_insert_has_no_returning_and_redacted_metadata() -> None:
    compiled = build_insert(**_kwargs()).compile(dialect=PG)
    sql = str(compiled)
    assert sql.startswith("INSERT INTO audit_events")
    assert "RETURNING" not in sql.upper()
    assert "clock_timestamp()" in sql
    params = compiled.params
    assert params["metadata"] == {"role": "member", "client_secret": REDACTED}
    assert params["event_type"] == "org.member.added"
    assert isinstance(params["id"], uuid.UUID)


def test_target_id_coerced_to_text() -> None:
    target = uuid.uuid4()
    assert _params(target_id=target)["target_id"] == str(target)


def test_cursor_round_trip_and_rejects_garbage() -> None:
    cursor = EventCursor(
        occurred_at=datetime(2026, 11, 16, 1, 2, 3, 456789, tzinfo=UTC), id=uuid.uuid4()
    )
    assert EventCursor.decode(cursor.encode()) == cursor
    for bad in ["", "!!!", "bm90LWEtY3Vyc29y", cursor.encode()[:-3]]:
        with pytest.raises(ValueError, match="invalid audit cursor"):
            EventCursor.decode(bad)
