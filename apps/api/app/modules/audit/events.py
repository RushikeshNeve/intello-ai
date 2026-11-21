"""Append-only security audit trail (M01.03.04, contract m01-contracts.md §5).

* :func:`record_event` writes into the caller's session, i.e. **in the same transaction** as the
  change it describes: a rolled-back change leaves no event behind.
* :func:`record_denied` opens its **own** tenant transaction and commits it, so a denial is kept
  even when the business transaction that discovered it rolls back.
* Inserts are plain ``INSERT`` statements without ``RETURNING``: the SELECT policy only shows the
  current org's rows, so ``RETURNING`` would fail for org-less events (DC-014).
* :func:`list_events` reads the current org's events (RLS restricts it; no org context -> empty).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, cast

from sqlalchemy import Engine, Insert, Table, func, insert, select, tuple_
from sqlalchemy.orm import Session

from app.core.db import tenant_session
from app.core.models import AUDIT_OUTCOMES, AuditEvent
from app.modules.audit.redaction import redact

Outcome = Literal["success", "denied", "error"]  # == models.AUDIT_OUTCOMES (unit-tested)

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200


class AuditEventType(StrEnum):
    """The closed set of event types (M01 + M02). Unknown types are rejected."""

    ORG_CREATED = "org.created"
    ORG_UPDATED = "org.updated"
    ORG_DELETED = "org.deleted"
    ORG_MEMBER_ADDED = "org.member.added"
    ORG_MEMBER_ROLE_CHANGED = "org.member.role_changed"
    ORG_MEMBER_REMOVED = "org.member.removed"
    ORG_MEMBER_PREAPPROVED = "org.member.preapproved"
    ORG_MEMBER_PREAPPROVAL_REMOVED = "org.member.preapproval_removed"
    WORKSPACE_CREATED = "workspace.created"
    WORKSPACE_UPDATED = "workspace.updated"
    WORKSPACE_DELETED = "workspace.deleted"
    WORKSPACE_MEMBER_ADDED = "workspace.member.added"
    WORKSPACE_MEMBER_ROLE_CHANGED = "workspace.member.role_changed"
    WORKSPACE_MEMBER_REMOVED = "workspace.member.removed"
    GRANT_CREATED = "grant.created"
    GRANT_REVOKED = "grant.revoked"
    AUTHZ_DENIED = "authz.denied"
    USER_PROVISIONED = "user.provisioned"
    DOCUMENT_CREATED = "document.created"
    DOCUMENT_VERSION_CREATED = "document.version.created"
    DOCUMENT_UPDATED = "document.updated"
    DOCUMENT_DELETED = "document.deleted"
    # M03 (contract §9): written by the publication transaction when a revision becomes searchable.
    DOCUMENT_VERSION_INDEXED = "document.version.indexed"
    # M03.03.04: a user re-queued a dead/unsupported ingestion job.
    DOCUMENT_INGESTION_RETRIED = "document.ingestion.retried"
    # M04: a search was performed (counts only, never the query text).
    SEARCH_PERFORMED = "search.performed"
    # M04 (contract §5, §8): counts and ids only, never question or answer text.
    ORG_AI_POLICY_UPDATED = "org.ai_policy.updated"
    CHAT_RUN_CREATED = "chat.run.created"
    CHAT_THREAD_DELETED = "chat.thread.deleted"
    # M06 (contract m06 section 2): ids only, never a token, secret or repository content.
    SOURCE_CREATED = "source.created"
    SOURCE_SYNC_REQUESTED = "source.sync.requested"
    SOURCE_REVOKED = "source.revoked"
    # M05: a workspace editor accepted or rejected an entity merge candidate (ids only).
    GRAPH_MERGE_DECIDED = "graph.merge.decided"
    # M07 (contract m07 sections 3, 4): ids and counts only, never memory or question text.
    MEMORY_SUGGESTED = "memory.suggested"
    MEMORY_APPROVED = "memory.approved"
    MEMORY_REJECTED = "memory.rejected"
    MEMORY_DELETED = "memory.deleted"
    RESEARCH_CREATED = "research.created"
    RESEARCH_CANCELLED = "research.cancelled"
    # M12.01: ids, kind and version numbers only, never prompt text.
    PROMPT_VERSION_CREATED = "prompt.version.created"
    PROMPT_VERSION_ACTIVATED = "prompt.version.activated"
    PROMPT_VERSION_ROLLED_BACK = "prompt.version.rolled_back"


class SessionOpener(Protocol):
    """Anything that opens a tenant transaction, e.g. ``app.core.db.TenantSessionFactory``."""

    def __call__(
        self, *, user_id: uuid.UUID | None, org_id: uuid.UUID | None
    ) -> AbstractContextManager[Session]: ...


def _event_type(value: str) -> AuditEventType:
    try:
        return AuditEventType(value)
    except ValueError:
        raise ValueError(f"unknown audit event type: {value!r}") from None


def _outcome(value: str) -> str:
    if value not in AUDIT_OUTCOMES:
        raise ValueError(f"invalid audit outcome: {value!r}")
    return value


def build_insert(
    *,
    event_type: str,
    actor_user_id: uuid.UUID | None,
    org_id: uuid.UUID | None,
    target_type: str,
    target_id: str,
    outcome: Outcome,
    request_id: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> Insert:
    """Validate and build the INSERT (no RETURNING). Raises ``ValueError`` on bad input."""
    kind = _event_type(event_type)
    result = _outcome(outcome)
    if not target_type:
        raise ValueError("target_type must not be empty")
    table = cast(Table, AuditEvent.__table__)
    return insert(table).values(
        id=uuid.uuid4(),
        # Wall-clock time of the event (now() would be the transaction start for every event).
        occurred_at=func.clock_timestamp(),
        org_id=org_id,
        actor_user_id=actor_user_id,
        event_type=kind.value,
        target_type=target_type,
        target_id=str(target_id),
        outcome=result,
        request_id=request_id,
        metadata=redact(metadata),
    )


def record_event(
    session: Session,
    *,
    event_type: str,
    actor_user_id: uuid.UUID | None,
    org_id: uuid.UUID | None,
    target_type: str,
    target_id: str,
    outcome: Outcome,
    request_id: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Write one audit event in ``session``'s transaction; it commits or rolls back with it.

    ``session`` must come from ``tenant_session`` (RLS context set): ``org_id`` must equal the
    context org, or be ``None`` with ``actor_user_id`` either ``None`` or the context user.
    """
    stmt = build_insert(
        event_type=event_type,
        actor_user_id=actor_user_id,
        org_id=org_id,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        request_id=request_id,
        metadata=metadata,
    )
    session.execute(stmt)


def record_denied(
    engine_or_factory: Engine | SessionOpener,
    *,
    actor_user_id: uuid.UUID | None,
    org_id: uuid.UUID | None,
    target_type: str,
    target_id: str,
    request_id: str | None,
    metadata: Mapping[str, Any] | None = None,
    event_type: str = AuditEventType.AUTHZ_DENIED,
    outcome: Outcome = "denied",
) -> None:
    """Write a denial (or other failure) event in its **own** committed transaction.

    Use it after (or instead of) the business transaction, never inside it, so a rollback of that
    transaction cannot lose the record. The RLS context is ``(actor_user_id, org_id)``. Database
    errors propagate: a denial that cannot be audited must not pass silently.
    """
    stmt = build_insert(
        event_type=event_type,
        actor_user_id=actor_user_id,
        org_id=org_id,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        request_id=request_id,
        metadata=metadata,
    )
    opener: AbstractContextManager[Session]
    if isinstance(engine_or_factory, Engine):
        opener = tenant_session(user_id=actor_user_id, org_id=org_id, engine=engine_or_factory)
    else:
        opener = engine_or_factory(user_id=actor_user_id, org_id=org_id)
    with opener as session:
        session.execute(stmt)


@dataclass(frozen=True)
class EventCursor:
    """Keyset position ``(occurred_at, id)``; ``encode()``/``decode()`` give an opaque string."""

    occurred_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.occurred_at.isoformat()}|{self.id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> EventCursor:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            stamp, ident = raw.split("|", 1)
            occurred_at = datetime.fromisoformat(stamp)
            if occurred_at.tzinfo is None:
                raise ValueError("naive timestamp")
            return cls(occurred_at=occurred_at, id=uuid.UUID(ident))
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ValueError("invalid audit cursor") from exc


@dataclass(frozen=True)
class AuditEventRecord:
    id: uuid.UUID
    occurred_at: datetime
    org_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    event_type: str
    target_type: str
    target_id: str
    outcome: str
    request_id: str | None
    metadata: dict[str, Any]

    @property
    def cursor(self) -> EventCursor:
        return EventCursor(occurred_at=self.occurred_at, id=self.id)


def list_events(
    session: Session, *, limit: int = DEFAULT_LIMIT, before: EventCursor | None = None
) -> list[AuditEventRecord]:
    """Newest-first events visible in ``session`` (RLS: the context org only).

    ``limit`` is clamped to ``1..MAX_LIMIT``. Pass the last record's ``cursor`` as ``before`` for
    the next page.
    """
    size = max(1, min(int(limit), MAX_LIMIT))
    stmt = select(AuditEvent).order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
    if before is not None:
        stmt = stmt.where(
            tuple_(AuditEvent.occurred_at, AuditEvent.id) < tuple_(before.occurred_at, before.id)
        )
    rows = session.execute(stmt.limit(size)).scalars().all()
    return [
        AuditEventRecord(
            id=row.id,
            occurred_at=row.occurred_at,
            org_id=row.org_id,
            actor_user_id=row.actor_user_id,
            event_type=row.event_type,
            target_type=row.target_type,
            target_id=row.target_id,
            outcome=row.outcome,
            request_id=row.request_id,
            metadata=dict(row.metadata_ or {}),
        )
        for row in rows
    ]
