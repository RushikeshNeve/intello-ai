"""Workspace overview counts (M09 backend gap 1): ``GET {ws}/overview``.

Counts only. Documents by ingestion state use the same permission-filtered statement as the jobs
list (never documents the caller cannot read); memories, research and chat are the caller's own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Tenant
from app.core.models import Document, Source
from app.modules.ai.models_db import ChatThread
from app.modules.documents import catalog_service as svc
from app.modules.ingestion.jobs_list import JobState, require_readable_workspace, state_counts
from app.modules.memory.models import Memory
from app.modules.research.models import ResearchSession

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["workspaces"])

RECENT_CHAT_DAYS: Final = 30


class OverviewResearch(BaseModel):
    by_state: dict[str, int]
    total: int


class Overview(BaseModel):
    documents_total: int  # every readable live document, including ones never processed
    documents: dict[JobState, int]
    sources: int
    pending_memories: int
    research: OverviewResearch
    recent_chat_threads: int


def _count(session: Session, stmt: Any) -> int:
    return int(session.execute(stmt).scalar_one())


def build_overview(session: Session, ctx: svc.WorkspaceCtx) -> Overview:
    org, ws, me = ctx.org_id, ctx.workspace_id, ctx.user_id
    sources = _count(
        session,
        select(func.count())
        .select_from(Source)
        .where(Source.org_id == org, Source.workspace_id == ws),
    )
    memories = _count(
        session,
        select(func.count())
        .select_from(Memory)
        .where(
            Memory.org_id == org,
            Memory.workspace_id == ws,
            Memory.owner_id == me,
            Memory.state == "suggested",
        ),
    )
    visible = (
        select(func.count())
        .select_from(Document)
        .where(Document.org_id == org, Document.workspace_id == ws, Document.deleted_at.is_(None))
    )
    if not ctx.sees_every_document:
        visible = visible.where(svc._visibility_filter(ctx))
    total = _count(session, visible)
    r = ResearchSession
    rows = session.execute(
        select(r.state, func.count())
        .where(r.org_id == org, r.workspace_id == ws, r.created_by == me)
        .group_by(r.state)
    ).all()
    by_state = {str(s): int(n) for s, n in rows}
    since = datetime.now(UTC) - timedelta(days=RECENT_CHAT_DAYS)
    chats = _count(
        session,
        select(func.count())
        .select_from(ChatThread)
        .where(
            ChatThread.org_id == org,
            ChatThread.workspace_id == ws,
            ChatThread.created_by == me,
            ChatThread.deleted_at.is_(None),
            ChatThread.updated_at >= since,
        ),
    )
    return Overview(
        documents_total=total,
        documents=state_counts(session, ctx),
        sources=sources,
        pending_memories=memories,
        research=OverviewResearch(by_state=by_state, total=sum(by_state.values())),
        recent_chat_threads=chats,
    )


@router.get("/overview", response_model=Overview)
def get_overview(tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID) -> Overview:
    with tenant.transaction(org_id) as session:
        ctx = require_readable_workspace(tenant, session, org_id, workspace_id)
        return build_overview(session, ctx)
