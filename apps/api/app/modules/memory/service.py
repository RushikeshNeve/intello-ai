"""Memory operations (M07.01.01-.03; contract m07 section 3).

A suggestion is stored ``suggested`` (the INSERT policy enforces it) and is never read by chat until
a human approves it. Approval of a ``workspace`` memory needs a workspace admin (org owner/admin or
workspace ``admin``). Everything runs in a tenant transaction (RLS: owner-private, workspace-visible
once approved) and filters explicitly as well. Nothing here logs or audits statement text.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import status
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.orm import Session

from app.api.deps import KeysetCursor, TenantRequest, api_error, not_found
from app.modules.ai.chat_service import _scope_or_none, require_member
from app.modules.audit import AuditEventType, record_event
from app.modules.memory import evidence as ev
from app.modules.memory.models import (
    STATE_APPROVED,
    STATE_INVALIDATED,
    STATE_REJECTED,
    STATE_SUGGESTED,
    STATES,
    Memory,
)
from app.modules.memory.schemas import (
    EvidenceOut,
    MemoryList,
    MemoryOut,
    SuggestionCreate,
)

TARGET_MEMORY: Final = "memory"
INVALID_EVIDENCE: Final = "invalid_evidence"
EVIDENCE_REQUIRED: Final = "evidence_required"
WORKSPACE_REQUIRED: Final = "workspace_required"
EVIDENCE_OUTSIDE_WORKSPACE: Final = "evidence_outside_workspace"
INVALID_EXPIRY: Final = "invalid_expiry"
INVALID_STATE: Final = "invalid_state"
EVIDENCE_STALE: Final = "evidence_stale"
FORBIDDEN: Final = "forbidden"
_REASON: Final = {
    ev.REASON_DELETED: "evidence_deleted",
    ev.REASON_SUPERSEDED: "evidence_superseded",
}


def _audit(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    event: AuditEventType,
    memory_id: uuid.UUID,
    **meta: Any,
) -> None:
    record_event(
        session,
        event_type=event,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=TARGET_MEMORY,
        target_id=str(memory_id),
        outcome="success",
        request_id=tenant.request_id,
        metadata={"memory_id": str(memory_id), **meta},
    )


def _out(row: Memory, states: list[ev.EvidenceState] | None = None) -> MemoryOut:
    by_chunk = {s.item.chunk_id: s for s in states or []}
    items: list[EvidenceOut] = []
    for item in ev.parse_items(row.evidence):
        s = by_chunk.get(item.chunk_id)
        items.append(
            EvidenceOut(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                version_id=item.version_id,
                workspace_id=item.workspace_id,
                evidence_status=None if s is None else s.status,
                title=None if s is None else s.title,
            )
        )
    return MemoryOut(
        id=row.id,
        workspace_id=row.workspace_id,
        owner_id=row.owner_id,
        visibility=row.visibility,  # type: ignore[arg-type]
        kind=row.kind,  # type: ignore[arg-type]
        statement=row.statement,
        state=row.state,  # type: ignore[arg-type]
        evidence=items,
        source=row.source,  # type: ignore[arg-type]
        source_ref=row.source_ref,
        expires_at=row.expires_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        invalidated_reason=row.invalidated_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _load(session: Session, org_id: uuid.UUID, memory_id: uuid.UUID) -> Memory | None:
    return session.execute(
        select(Memory).where(
            Memory.id == memory_id, Memory.org_id == org_id, Memory.deleted_at.is_(None)
        )
    ).scalar_one_or_none()


def _readable(session: Session, org_id: uuid.UUID, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    scope = _scope_or_none(session, org_id, user_id)
    return frozenset() if scope is None else scope.workspace_ids


# -------------------------------------------------------------------------------------- create


def suggest(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, body: SuggestionCreate
) -> MemoryOut:
    require_member(session, tenant, org_id)
    if body.visibility == "workspace" and body.workspace_id is None:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, WORKSPACE_REQUIRED)
    if body.kind != "preference" and not body.evidence:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, EVIDENCE_REQUIRED)
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_EXPIRY)
    scope = _scope_or_none(session, org_id, tenant.user_id)
    if scope is None or (
        body.workspace_id is not None and body.workspace_id not in scope.workspace_ids
    ):
        raise not_found()  # a workspace the caller cannot read does not exist for them
    items = ev.validate_for_creation(
        session,
        scope,
        [ev.EvidenceItem(e.chunk_id, e.document_id, e.version_id) for e in body.evidence],
    )
    if items is None:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_EVIDENCE)
    if body.visibility == "workspace" and any(i.workspace_id != body.workspace_id for i in items):
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, EVIDENCE_OUTSIDE_WORKSPACE)
    memory_id = uuid.uuid4()
    session.execute(
        insert(Memory).values(
            id=memory_id,
            org_id=org_id,
            workspace_id=body.workspace_id,
            owner_id=tenant.user_id,
            visibility=body.visibility,
            kind=body.kind,
            statement=body.statement,
            state=STATE_SUGGESTED,
            evidence=[i.to_json() for i in items],
            source=body.source,
            source_ref=body.source_ref,
            expires_at=body.expires_at,
        )
    )
    row = _load(session, org_id, memory_id)
    if row is None:  # pragma: no cover - inserted an instant ago in this transaction
        raise not_found()
    _audit(
        session,
        tenant,
        org_id,
        AuditEventType.MEMORY_SUGGESTED,
        memory_id,
        kind=body.kind,
        visibility=body.visibility,
        evidence_count=len(items),
        statement_chars=len(body.statement),
    )
    return _out(row)


# ---------------------------------------------------------------------------------------- read


def list_memories(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    *,
    state: str | None,
    limit: int,
    after: KeysetCursor | None,
) -> MemoryList:
    require_member(session, tenant, org_id)
    if state is not None and state not in STATES:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_STATE)
    stmt = (
        select(Memory)
        .where(Memory.org_id == org_id, Memory.deleted_at.is_(None))
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .limit(limit + 1)
    )
    if state is not None:
        stmt = stmt.where(Memory.state == state)
    if after is not None:
        stmt = stmt.where(
            (Memory.created_at < after.created_at)
            | ((Memory.created_at == after.created_at) & (Memory.id < after.id))
        )
    rows = list(session.execute(stmt).scalars())
    cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        cursor = KeysetCursor(created_at=rows[-1].created_at, id=rows[-1].id).encode()
    return MemoryList(items=[_out(r) for r in rows], next_cursor=cursor)


def _refresh(
    session: Session, org_id: uuid.UUID, row: Memory, readable: frozenset[uuid.UUID]
) -> tuple[Memory, list[ev.EvidenceState]]:
    """Re-resolve the evidence now; an evidence fact that changed invalidates the memory (lazy
    invalidation, for every owner). Returns the (possibly updated) row and the item states."""
    states = ev.check(session, org_id, ev.parse_items(row.evidence), readable)
    if row.state in (STATE_SUGGESTED, STATE_APPROVED):
        for reason in (ev.REASON_DELETED, ev.REASON_SUPERSEDED):
            docs = {s.item.document_id for s in states if s.reason == reason}
            if docs:
                ev.invalidate_documents(session, org_id, docs, _REASON[reason])
                refreshed = _load(session, org_id, row.id)
                if refreshed is not None:
                    session.refresh(refreshed)
                    row = refreshed
                break
    return row, states


def get_memory(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, memory_id: uuid.UUID
) -> MemoryOut:
    require_member(session, tenant, org_id)
    row = _load(session, org_id, memory_id)
    if row is None:
        raise not_found()
    row, states = _refresh(session, org_id, row, _readable(session, org_id, tenant.user_id))
    return _out(row, states)


# ------------------------------------------------------------------------------------ decisions


def _ws_access(session: Session, workspace_id: uuid.UUID | None) -> str | None:
    if workspace_id is None:
        return None
    value = session.execute(text("SELECT memory_ws_access(:w)"), {"w": workspace_id}).scalar()
    return None if value is None else str(value)


def _decide(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    memory_id: uuid.UUID,
    *,
    new_state: str,
) -> MemoryOut:
    require_member(session, tenant, org_id)
    row = _load(session, org_id, memory_id)
    if row is None:
        raise not_found()
    if row.visibility == "workspace":
        if _ws_access(session, row.workspace_id) != "admin":
            raise api_error(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    elif row.owner_id != tenant.user_id:  # pragma: no cover - RLS already hides it
        raise not_found()
    if row.state != STATE_SUGGESTED:
        raise api_error(status.HTTP_409_CONFLICT, INVALID_STATE)
    if new_state == STATE_APPROVED:
        row, states = _refresh(session, org_id, row, _readable(session, org_id, tenant.user_id))
        if row.state == STATE_INVALIDATED or any(s.status != "ok" for s in states):
            raise api_error(status.HTTP_409_CONFLICT, EVIDENCE_STALE)
        if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
            raise api_error(status.HTTP_409_CONFLICT, INVALID_STATE)
    values: dict[str, Any] = {"state": new_state, "updated_at": func.now()}
    if new_state == STATE_APPROVED:
        values.update(approved_by=tenant.user_id, approved_at=func.now())
    updated = session.execute(
        update(Memory)
        .where(Memory.id == memory_id, Memory.state == STATE_SUGGESTED)
        .values(**values)
        .returning(Memory.id)
    ).scalar_one_or_none()
    if updated is None:
        raise api_error(status.HTTP_409_CONFLICT, INVALID_STATE)
    session.expire_all()
    fresh = _load(session, org_id, memory_id)
    if fresh is None:  # pragma: no cover
        raise not_found()
    _audit(
        session,
        tenant,
        org_id,
        AuditEventType.MEMORY_APPROVED
        if new_state == STATE_APPROVED
        else AuditEventType.MEMORY_REJECTED,
        memory_id,
        visibility=fresh.visibility,
    )
    return _out(fresh)


def approve(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, memory_id: uuid.UUID
) -> MemoryOut:
    return _decide(session, tenant, org_id, memory_id, new_state=STATE_APPROVED)


def reject(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, memory_id: uuid.UUID
) -> MemoryOut:
    return _decide(session, tenant, org_id, memory_id, new_state=STATE_REJECTED)


def delete(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, memory_id: uuid.UUID
) -> None:
    """Owner-only soft delete (``deleted_at``); nobody else's memory is addressable."""
    require_member(session, tenant, org_id)
    updated = session.execute(
        update(Memory)
        .where(
            Memory.id == memory_id,
            Memory.org_id == org_id,
            Memory.owner_id == tenant.user_id,
            Memory.deleted_at.is_(None),
        )
        .values(deleted_at=func.now(), updated_at=func.now())
        .returning(Memory.id)
    ).scalar_one_or_none()
    if updated is None:
        raise not_found()
    _audit(session, tenant, org_id, AuditEventType.MEMORY_DELETED, memory_id)
