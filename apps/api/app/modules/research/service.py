"""API-side research operations (M07.02; contract m07 section 4).

Sessions are private to their creator (RLS + explicit filters; another member's session is a
concealed 404). The scope is derived here once, with the caller's session, and stored as a
snapshot; the worker re-resolves it when it executes. Limits are validated BEFORE any work is
queued. Reports are read through the chat citation re-resolution: a statement whose citations no
longer resolve (document deleted, workspace lost) is dropped from the response.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Final

from fastapi import status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import KeysetCursor, TenantRequest, api_error, not_found
from app.core.config import Settings
from app.modules.ai.chat_schemas import CitationOut
from app.modules.ai.chat_service import _citation_outs, _scope_or_none, require_member
from app.modules.audit import AuditEventType, record_event
from app.modules.research import planner, repo
from app.modules.research.models import ResearchSession
from app.modules.research.runtime import error_message
from app.modules.research.schemas import (
    CancelOut,
    PlanOut,
    ReportOut,
    ResearchAccepted,
    ResearchCreate,
    ResearchList,
    ResearchOut,
    ResearchSummary,
    SectionOut,
    StatementOut,
    StepOut,
)

TARGET_RESEARCH: Final = "research_session"
LIMIT_EXCEEDED: Final = "limit_exceeded"
TOO_MANY_RESEARCH: Final = "too_many_research"
REPORT_NOT_READY: Final = "report_not_ready"
MAX_ACTIVE_PER_USER: Final = 2
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def create(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    body: ResearchCreate,
    settings: Settings,
) -> ResearchAccepted:
    require_member(session, tenant, org_id)
    try:
        limits = planner.effective_limits(
            settings, body.limits.model_dump() if body.limits is not None else None
        )
    except planner.LimitExceeded:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, LIMIT_EXCEEDED) from None
    requested = set(body.workspace_ids) if body.workspace_ids else None
    scope = _scope_or_none(session, org_id, tenant.user_id, requested)
    if scope is None or (requested is not None and not requested <= scope.workspace_ids):
        raise not_found()  # a workspace the caller cannot read does not exist for them
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"research:{tenant.user_id}"},
    )
    if repo.count_active(session, user_id=tenant.user_id) >= MAX_ACTIVE_PER_USER:
        raise api_error(status.HTTP_409_CONFLICT, TOO_MANY_RESEARCH)
    snapshot: dict[str, Any] = {"workspace_ids": sorted(str(w) for w in scope.workspace_ids)}
    session_id = repo.create_session(
        session,
        org_id=org_id,
        user_id=tenant.user_id,
        workspace_id=next(iter(requested))
        if requested is not None and len(requested) == 1
        else None,
        question=body.question,
        limits=limits,
        scope_snapshot=snapshot,
    )
    record_event(
        session,
        event_type=AuditEventType.RESEARCH_CREATED,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=TARGET_RESEARCH,
        target_id=str(session_id),
        outcome="success",
        request_id=tenant.request_id,
        metadata={
            "session_id": str(session_id),
            "workspace_count": len(snapshot["workspace_ids"]),
            "question_chars": len(body.question),
            "max_subqueries": limits["max_subqueries"],
        },
    )
    return ResearchAccepted(id=session_id, state="planned")


def _load(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, session_id: uuid.UUID
) -> ResearchSession:
    require_member(session, tenant, org_id)
    row = repo.get_session(session, org_id=org_id, user_id=tenant.user_id, session_id=session_id)
    if row is None:
        raise not_found()
    return row


def _report_out(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, session_id: uuid.UUID
) -> ReportOut | None:
    report = repo.get_report(session, session_id)
    if report is None:
        return None
    scope = _scope_or_none(session, org_id, tenant.user_id)
    sections: list[SectionOut] = []
    dropped = 0
    for sec in report.sections:
        statements: list[StatementOut] = []
        for st in sec.get("statements", []):
            cites: list[CitationOut] = _citation_outs(session, scope, st.get("citations", []))
            if cites:
                statements.append(StatementOut(text=str(st.get("text", "")), citations=cites))
            else:
                dropped += 1
        sections.append(SectionOut(heading=str(sec.get("heading", "")), statements=statements))
    verification = dict(report.verification)
    if dropped:
        verification["withheld_at_read"] = dropped
    return ReportOut(sections=sections, verification=verification, created_at=report.created_at)


def detail(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, session_id: uuid.UUID
) -> ResearchOut:
    row = _load(session, tenant, org_id, session_id)
    steps = repo.list_steps(session, row.id)
    plan = row.plan.get("subqueries") if isinstance(row.plan, dict) else None
    return ResearchOut(
        id=row.id,
        question=row.question,
        state=row.state,  # type: ignore[arg-type]
        plan=PlanOut(subqueries=[str(q) for q in plan or []]),
        steps=[StepOut(seq=s.seq, kind=s.kind, query=s.query, state=s.state) for s in steps],  # type: ignore[arg-type]
        usage=dict(row.usage),
        limits=dict(row.limits),
        error_code=row.error_code,
        message=error_message(row.error_code) if row.error_code else None,
        report=_report_out(session, tenant, org_id, row.id) if row.state == "completed" else None,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


def list_all(
    session: Session,
    tenant: TenantRequest,
    org_id: uuid.UUID,
    *,
    limit: int,
    after: KeysetCursor | None,
) -> ResearchList:
    require_member(session, tenant, org_id)
    rows, cursor = repo.list_sessions(
        session, org_id=org_id, user_id=tenant.user_id, limit=limit, after=after
    )
    return ResearchList(
        items=[
            ResearchSummary(
                id=r.id,
                question=r.question,
                state=r.state,  # type: ignore[arg-type]
                error_code=r.error_code,
                created_at=r.created_at,
                finished_at=r.finished_at,
            )
            for r in rows
        ],
        next_cursor=None if cursor is None else cursor.encode(),
    )


def cancel(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, session_id: uuid.UUID
) -> CancelOut:
    require_member(session, tenant, org_id)
    result = repo.cancel_session(session, session_id)
    if result is None:
        raise not_found()
    state, cancelled = result
    if cancelled:
        record_event(
            session,
            event_type=AuditEventType.RESEARCH_CANCELLED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type=TARGET_RESEARCH,
            target_id=str(session_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata={"session_id": str(session_id)},
        )
    return CancelOut(state=state)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------- export


def _md(text: str) -> str:
    """Text for markdown output: control characters removed and angle brackets escaped, so the
    document never carries raw HTML."""
    return _CONTROL.sub("", text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_markdown(
    session: Session, tenant: TenantRequest, org_id: uuid.UUID, session_id: uuid.UUID
) -> str:
    row = _load(session, tenant, org_id, session_id)
    report = _report_out(session, tenant, org_id, row.id) if row.state == "completed" else None
    if report is None:
        raise api_error(status.HTTP_409_CONFLICT, REPORT_NOT_READY)
    lines = ["# Research report", "", f"**Question:** {_md(' '.join(row.question.split()))}", ""]
    footnotes: dict[str, str] = {}
    for sec in report.sections:
        lines += [f"## {_md(sec.heading)}", ""]
        if not sec.statements:
            lines += ["_No supported statements._", ""]
        for st in sec.statements:
            refs = ""
            for c in st.citations:
                label = c.marker.strip("[]")
                refs += f"[^{label}]"
                pages = (
                    ""
                    if c.page_start is None
                    else f", p. {c.page_start}"
                    + (f"-{c.page_end}" if c.page_end not in (None, c.page_start) else "")
                )
                quote = _md(" ".join(c.excerpt.split()))
                footnotes.setdefault(
                    label, f'{_md(c.title)} (v{c.version_number}{pages}): "{quote}"'
                )
            lines += [f"{_md(st.text)} {refs}".rstrip(), ""]
    for label in sorted(footnotes, key=int):
        lines.append(f"[^{label}]: {footnotes[label]}")
    status_line = str(report.verification.get("status", ""))
    lines += ["", f"_Verification: {_md(status_line)}._", ""]
    return "\n".join(lines)
