"""Answer feedback (M13.03, O5): thumbs + reason code on an assistant message; no free text.

* ``PUT /v1/orgs/{o}/chat/messages/{message_id}/feedback``   set/replace the caller's vote
* ``GET /v1/orgs/{o}/chat/messages/{message_id}/feedback``   the caller's vote (``null`` if none)
* ``GET /v1/orgs/{o}/feedback/review-queue``                 org owners/admins: down-voted runs with
  the chunk ids behind them (golden-set review queue shape; the review UI is a later task)

A message the caller cannot see (another user's thread, another org) is a concealed 404. RLS lets a
user read only their own votes; owners/admins read the org's.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, not_found
from app.modules.ai.chat_service import require_member
from app.modules.authorization.policy import Action
from app.modules.organizations import service as org_service

router = APIRouter(prefix="/v1/orgs/{org_id}", tags=["feedback"])

TARGET = "answer_feedback"
Reason = Literal["wrong_answer", "wrong_citation", "missing_info", "unsafe", "other"]


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    reason: Reason | None = None

    @model_validator(mode="after")
    def _reason_only_for_down(self) -> FeedbackIn:
        if self.reason is not None and self.rating != "down":
            raise ValueError("reason is only allowed with a down vote")
        return self


class FeedbackOut(BaseModel):
    message_id: uuid.UUID
    run_id: uuid.UUID
    rating: str
    reason: str | None
    created_at: datetime


class ReviewItem(BaseModel):
    feedback_id: uuid.UUID
    run_id: uuid.UUID
    message_id: uuid.UUID
    workspace_id: uuid.UUID | None
    reason: str | None
    chunk_ids: list[uuid.UUID]
    created_at: datetime


class ReviewQueue(BaseModel):
    items: list[ReviewItem]


def _message(session: Session, message_id: uuid.UUID) -> dict[str, object] | None:
    row = (
        session.execute(
            text(
                "SELECT m.id, m.run_id, t.workspace_id, m.citations FROM chat_messages m"
                " JOIN chat_threads t ON t.id = m.thread_id AND t.org_id = m.org_id"
                " WHERE m.id = :m AND m.role = 'assistant' AND m.run_id IS NOT NULL"
                " AND t.deleted_at IS NULL"
            ),
            {"m": message_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


def _chunk_ids(citations: object) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for item in citations if isinstance(citations, list) else []:
        try:
            out.append(uuid.UUID(str(item["chunk_id"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


_OUT = "message_id, run_id, rating, reason, created_at"


@router.put("/chat/messages/{message_id}/feedback", response_model=FeedbackOut)
def put_feedback(
    tenant: Tenant, org_id: uuid.UUID, message_id: uuid.UUID, body: FeedbackIn
) -> FeedbackOut:
    with tenant.transaction(org_id) as session:
        require_member(session, tenant, org_id)
        msg = _message(session, message_id)
        if msg is None:
            raise not_found()
        row = (
            session.execute(
                text(
                    "INSERT INTO answer_feedback (org_id, workspace_id, run_id, message_id,"  # noqa: S608
                    " user_id, rating, reason, chunk_ids)"
                    " VALUES (:o, :w, :r, :m, :u, :rating, :reason, :c)"
                    " ON CONFLICT (message_id, user_id) DO UPDATE SET rating = EXCLUDED.rating,"
                    " reason = EXCLUDED.reason, chunk_ids = EXCLUDED.chunk_ids,"
                    " created_at = now()"
                    f" RETURNING {_OUT}"
                ),
                {
                    "o": org_id,
                    "w": msg["workspace_id"],
                    "r": msg["run_id"],
                    "m": message_id,
                    "u": tenant.user_id,
                    "rating": body.rating,
                    "reason": body.reason,
                    "c": _chunk_ids(msg["citations"]),
                },
            )
            .mappings()
            .one()
        )
    return FeedbackOut(**row)


@router.get("/chat/messages/{message_id}/feedback", response_model=FeedbackOut | None)
def get_feedback(tenant: Tenant, org_id: uuid.UUID, message_id: uuid.UUID) -> FeedbackOut | None:
    with tenant.transaction(org_id) as session:
        require_member(session, tenant, org_id)
        if _message(session, message_id) is None:
            raise not_found()
        row = (
            session.execute(
                text(f"SELECT {_OUT} FROM answer_feedback WHERE message_id = :m AND user_id = :u"),  # noqa: S608
                {"m": message_id, "u": tenant.user_id},
            )
            .mappings()
            .one_or_none()
        )
    return None if row is None else FeedbackOut(**row)


def _admin(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> None:
    access = org_service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    access.require(Action.ORG_UPDATE, target_type=TARGET)


@router.get("/feedback/review-queue", response_model=ReviewQueue)
def review_queue(
    tenant: Tenant, org_id: uuid.UUID, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> ReviewQueue:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        rows = session.execute(
            text(
                "SELECT f.id AS feedback_id, f.run_id, f.message_id, f.workspace_id, f.reason,"
                " f.created_at, ARRAY(SELECT DISTINCT x FROM unnest("
                "   f.chunk_ids || COALESCE(c.chunk_ids, '{}')) AS x ORDER BY x) AS chunk_ids"
                " FROM answer_feedback f"
                " LEFT JOIN ai_run_captures c ON c.run_id = f.run_id AND c.org_id = f.org_id"
                " WHERE f.org_id = :o AND f.rating = 'down'"
                " ORDER BY f.created_at DESC, f.id LIMIT :n"
            ),
            {"o": org_id, "n": limit},
        ).mappings()
        items = [ReviewItem(**r) for r in rows]
    return ReviewQueue(items=items)
