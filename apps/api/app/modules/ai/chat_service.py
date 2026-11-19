"""API-side chat operations (M04.02.04; contract m04 §8 and §14).

Threads, runs and messages are private to their creator: every query runs in a tenant transaction
(RLS) AND filters by the caller, so another org member's thread/run is simply absent (the router
answers a concealed 404). The scope of a run is derived here, with the caller's session, once, and
stored as a snapshot; the worker re-resolves it when it executes.

Nothing here puts a question or an answer into logs or audit rows (ids and counts only).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any, Final

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import TenantRequest, api_error, not_found
from app.core.config import Settings
from app.modules.ai import chat_repo
from app.modules.ai.chat_repo import MessageRow, RunLimitExceeded, ThreadRow
from app.modules.ai.chat_schemas import (
    ActiveRun,
    CitationOut,
    MessageOut,
    RunOut,
    ThreadDetail,
    ThreadOut,
    VerificationOut,
)
from app.modules.ai.models_db import ACTIVE_RUN_STATES, ChatRun
from app.modules.ai.prompts import PROMPT_VERSION
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.organizations.service import load_org_access
from app.modules.retrieval.citations import CitationRef, resolve_citation
from app.modules.retrieval.scope import EmptyScope, RetrievalScope, resolve_scope

TARGET_CHAT: Final = "chat"
TARGET_CHAT_THREAD: Final = "chat_thread"
TARGET_CHAT_RUN: Final = "chat_run"

RATE_LIMITED: Final = "rate_limited"
TOO_MANY_RUNS: Final = "too_many_runs"
RUN_IN_PROGRESS: Final = "run_in_progress"


def require_member(session: Session, tenant: TenantRequest, org_id: uuid.UUID) -> None:
    """Org member or a (concealed, audited) 404."""
    access = load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    access.require(Action.ORG_READ, target_type=TARGET_CHAT)


def thread_out(row: ThreadRow) -> ThreadOut:
    return ThreadOut(
        id=row.id,
        workspace_id=row.workspace_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _scope_or_none(
    session: Session,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    requested: set[uuid.UUID] | None = None,
) -> RetrievalScope | None:
    try:
        return resolve_scope(
            session, org_id=org_id, user_id=user_id, requested_workspace_ids=requested
        )
    except EmptyScope:
        return None


# ------------------------------------------------------------------------------------ messages


def _citation_outs(
    session: Session, scope: RetrievalScope | None, stored: list[Any]
) -> list[CitationOut]:
    """Stored citation refs, re-resolved NOW: a deleted document or a lost workspace drops out and
    the title/version are current. The excerpt is the one stored at answer time (<= 240 chars)."""
    if scope is None:
        return []
    out: list[CitationOut] = []
    cache: dict[tuple[str, str, str], Any] = {}
    for item in stored:
        if not isinstance(item, dict):
            continue
        try:
            ref = CitationRef(
                chunk_id=uuid.UUID(str(item["chunk_id"])),
                document_id=uuid.UUID(str(item["document_id"])),
                version_id=uuid.UUID(str(item["version_id"])),
            )
        except (KeyError, ValueError):
            continue
        key = (str(ref.chunk_id), str(ref.document_id), str(ref.version_id))
        if key not in cache:
            cache[key] = resolve_citation(session, scope, ref)
        resolved = cache[key]
        if resolved is None:
            continue
        out.append(
            CitationOut(
                marker=str(item.get("marker", "")),
                chunk_id=ref.chunk_id,
                document_id=ref.document_id,
                version_id=ref.version_id,
                workspace_id=resolved.workspace_id,
                title=resolved.title,
                version_number=resolved.version_number,
                page_start=item.get("page_start"),
                page_end=item.get("page_end"),
                excerpt=str(item.get("excerpt", ""))[:240],
            )
        )
    return out


def message_out(session: Session, scope: RetrievalScope | None, row: MessageRow) -> MessageOut:
    verification: VerificationOut | None = None
    if row.verification is not None:
        verification = VerificationOut(
            status=row.verification["status"],
            withheld_count=int(row.verification.get("withheld_count", 0)),
            flagged_count=int(row.verification.get("flagged_count", 0)),
        )
    return MessageOut(
        id=row.id,
        role="assistant" if row.role == "assistant" else "user",
        content=row.content,
        citations=_citation_outs(session, scope, row.citations) if row.role == "assistant" else [],
        verification=verification,
        run_id=row.run_id,
        created_at=row.created_at,
    )


# -------------------------------------------------------------------------------------- threads


def create_thread(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None,
    title: str,
) -> ThreadRow:
    require_member(session, tenant, org_id)
    if workspace_id is not None and (
        _scope_or_none(session, org_id, tenant.user_id, {workspace_id}) is None
    ):
        raise not_found()  # a workspace the caller cannot read does not exist for them
    return chat_repo.create_thread(
        session, org_id=org_id, user_id=tenant.user_id, workspace_id=workspace_id, title=title
    )


def thread_detail(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, thread_id: uuid.UUID
) -> ThreadDetail:
    require_member(session, tenant, org_id)
    thread = chat_repo.get_thread(
        session, org_id=org_id, user_id=tenant.user_id, thread_id=thread_id
    )
    if thread is None:
        raise not_found()
    scope = _scope_or_none(session, org_id, tenant.user_id)
    messages = [
        message_out(session, scope, m)
        for m in chat_repo.list_messages(session, org_id=org_id, thread_id=thread_id)
    ]
    active = session.execute(
        select(ChatRun.id, ChatRun.state)
        .where(
            ChatRun.thread_id == thread_id,
            ChatRun.created_by == tenant.user_id,
            ChatRun.state.in_(ACTIVE_RUN_STATES),
        )
        .order_by(ChatRun.created_at.desc())
        .limit(1)
    ).one_or_none()
    return ThreadDetail(
        thread=thread_out(thread),
        messages=messages,
        active_run=None if active is None else ActiveRun(run_id=active.id, state=active.state),
    )


def delete_thread(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, thread_id: uuid.UUID
) -> None:
    require_member(session, tenant, org_id)
    if not chat_repo.soft_delete_thread(
        session, org_id=org_id, user_id=tenant.user_id, thread_id=thread_id
    ):
        raise not_found()
    record_event(
        session,
        event_type=AuditEventType.CHAT_THREAD_DELETED,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=TARGET_CHAT_THREAD,
        target_id=str(thread_id),
        outcome="success",
        request_id=tenant.request_id,
        metadata={"thread_id": str(thread_id)},
    )


# ---------------------------------------------------------------------------------------- runs


@dataclass(frozen=True, slots=True)
class AcceptedRun:
    run_id: uuid.UUID
    message_id: uuid.UUID
    state: str
    created: bool


def _run_state(session: Session, run_id: uuid.UUID) -> str:
    return str(session.execute(select(ChatRun.state).where(ChatRun.id == run_id)).scalar_one())


def retry_after_seconds(settings: Settings) -> int:
    """Per-minute limit hit: wait about one slot of the window (never less than a second)."""
    return max(1, math.ceil(60 / settings.CHAT_MAX_RUNS_PER_MIN))


def create_run(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    thread_id: uuid.UUID,
    *,
    content: str,
    idempotency_key: str,
    workspace_ids: list[uuid.UUID] | None,
    settings: Settings,
) -> AcceptedRun:
    """Create (or replay) a run. The same ``(thread, idempotency_key)`` always yields the same run;
    a replay is answered before limits and the one-active-run rule. Raises ``HTTPException`` for the
    concealed 404 (no such thread), 409 ``run_in_progress`` / ``too_many_runs`` and 429
    ``rate_limited``."""
    require_member(session, tenant, org_id)
    thread = chat_repo.get_thread(
        session, org_id=org_id, user_id=tenant.user_id, thread_id=thread_id
    )
    if thread is None:
        raise not_found()
    # Serialise this user's run creation (create_run re-takes the same lock, which is re-entrant).
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"chat_runs:{tenant.user_id}"},
    )
    existing = session.execute(
        select(ChatRun.id, ChatRun.user_message_id, ChatRun.state).where(
            ChatRun.thread_id == thread_id, ChatRun.idempotency_key == idempotency_key
        )
    ).one_or_none()
    if existing is not None:
        return AcceptedRun(existing.id, existing.user_message_id, existing.state, created=False)
    busy = session.execute(
        select(ChatRun.id)
        .where(ChatRun.thread_id == thread_id, ChatRun.state.in_(ACTIVE_RUN_STATES))
        .limit(1)
    ).first()
    if busy is not None:
        raise api_error(status.HTTP_409_CONFLICT, RUN_IN_PROGRESS)

    requested: set[uuid.UUID] | None = None
    if thread.workspace_id is not None:
        requested = {thread.workspace_id}
    if workspace_ids is not None:
        asked = set(workspace_ids)
        requested = asked if requested is None else requested & asked
    scope = _scope_or_none(session, org_id, tenant.user_id, requested)
    snapshot: dict[str, Any] = {
        "workspace_ids": [] if scope is None else sorted(str(w) for w in scope.workspace_ids),
        "requested": None if requested is None else sorted(str(w) for w in requested),
    }
    try:
        created = chat_repo.create_run(
            session,
            org_id=org_id,
            user_id=tenant.user_id,
            thread_id=thread_id,
            content=content,
            idempotency_key=idempotency_key,
            scope_snapshot=snapshot,
            model={"prompt_version": PROMPT_VERSION},
            max_concurrent=settings.CHAT_MAX_CONCURRENT_RUNS,
            max_per_minute=settings.CHAT_MAX_RUNS_PER_MIN,
        )
    except RunLimitExceeded as exc:
        if exc.kind == "concurrent":
            raise api_error(status.HTTP_409_CONFLICT, TOO_MANY_RUNS) from None
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            RATE_LIMITED,
            headers={"Retry-After": str(retry_after_seconds(settings))},
        ) from None
    if created.created:
        record_event(
            session,
            event_type=AuditEventType.CHAT_RUN_CREATED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type=TARGET_CHAT_RUN,
            target_id=str(created.run_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata={
                "thread_id": str(thread_id),
                "workspace_count": len(snapshot["workspace_ids"]),
                "narrowed": requested is not None,
                "question_chars": len(content),
            },
        )
    return AcceptedRun(
        created.run_id, created.message_id, _run_state(session, created.run_id), created.created
    )


def run_detail(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, run_id: uuid.UUID
) -> RunOut:
    require_member(session, tenant, org_id)
    run = chat_repo.get_run(session, org_id=org_id, user_id=tenant.user_id, run_id=run_id)
    if run is None:
        raise not_found()
    message: MessageOut | None = None
    if run.state == "completed":
        for m in chat_repo.list_messages(session, org_id=org_id, thread_id=run.thread_id):
            if m.run_id == run.id and m.role == "assistant":
                scope = _scope_or_none(session, org_id, tenant.user_id)
                message = message_out(session, scope, m)
                break
    return RunOut(
        run_id=run.id,
        thread_id=run.thread_id,
        state=run.state,
        error_code=run.error_code,
        message=message,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


def cancel_run(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, run_id: uuid.UUID
) -> str:
    require_member(session, tenant, org_id)
    result = chat_repo.cancel_run(session, run_id)
    if result is None:
        raise not_found()
    return result.state
