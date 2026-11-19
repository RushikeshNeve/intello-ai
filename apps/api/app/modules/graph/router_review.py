"""Entity merge review API (M05.01.03, contract m05 section 3).

* ``GET  /v1/orgs/{o}/workspaces/{w}/graph/merge-candidates``   pending candidates plus the
  same-name/different-type conflicts (kept separate), ``document.write`` (workspace editor+)
* ``POST .../graph/merge-candidates/{id}/decision``   accept or reject, audited

A viewer gets 403 (audited), a non-member or an id of another workspace/org a concealed 404, a
decided candidate or an entity merged elsewhere 409. The decision commits in PostgreSQL first; the
RushDB claims are re-pointed afterwards (best effort; extraction follows ``merged_into``).
"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import CONFLICT, Tenant, api_error, not_found
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.graph import resolve
from app.modules.graph.models import GraphEntity
from app.modules.knowledge.ports import (
    GraphRepository,
    KnowledgeStoreError,
    UnknownOrganization,
    validated_org_scope,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}/graph", tags=["graph"])

TARGET_CANDIDATE = "graph_merge_candidate"


class EntityOut(BaseModel):
    id: uuid.UUID
    type: str
    canonical_name: str
    aliases: list[str]


class MergeCandidateOut(BaseModel):
    id: uuid.UUID
    score: float
    state: str
    entity_a: EntityOut
    entity_b: EntityOut


class ConflictOut(BaseModel):
    normalized_name: str
    entities: list[EntityOut]


class MergeCandidatesOut(BaseModel):
    items: list[MergeCandidateOut]
    conflicts: list[ConflictOut]


class DecisionIn(BaseModel):
    decision: Literal["accept", "reject"]


class DecisionOut(BaseModel):
    id: uuid.UUID
    state: str
    merged_into: uuid.UUID | None = None


def _entity(e: GraphEntity) -> EntityOut:
    return EntityOut(
        id=e.id,
        type=e.type,
        canonical_name=e.canonical_name,
        aliases=[str(a) for a in e.aliases],
    )


@lru_cache(maxsize=1)
def _default_graph_repository() -> GraphRepository:
    from app.infrastructure.rushdb.graph import RushDbGraphRepository
    from app.infrastructure.rushdb.settings import RushDbSettings

    return RushDbGraphRepository(RushDbSettings())


def get_graph_repository() -> GraphRepository | None:
    """``None`` when the index is not configured: the decision still stands in PostgreSQL."""
    try:
        return _default_graph_repository()
    except Exception:  # noqa: BLE001 - misconfiguration must not surface as a 500
        return None


GraphRepoDep = Annotated[GraphRepository | None, Depends(get_graph_repository)]


@router.get("/merge-candidates", response_model=MergeCandidatesOut)
def list_merge_candidates(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    state: Literal["pending", "accepted", "rejected"] = "pending",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MergeCandidatesOut:
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_WRITE)
        ctx.require(
            Action.DOCUMENT_WRITE, target_type=TARGET_CANDIDATE, target_id=str(workspace_id)
        )
        items = [
            MergeCandidateOut(
                id=c.id,
                score=c.score,
                state=c.state,
                entity_a=_entity(c.entity_a),
                entity_b=_entity(c.entity_b),
            )
            for c in resolve.list_candidates(session, workspace_id, state=state, limit=limit)
        ]
        conflicts = [
            ConflictOut(normalized_name=name, entities=[_entity(e) for e in members])
            for name, members in resolve.list_conflicts(session, workspace_id, limit=limit)
        ]
    return MergeCandidatesOut(items=items, conflicts=conflicts)


@router.post("/merge-candidates/{candidate_id}/decision", response_model=DecisionOut)
def decide_merge_candidate(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: DecisionIn,
    graph_repo: GraphRepoDep,
) -> DecisionOut:
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_WRITE)
        ctx.require(
            Action.DOCUMENT_WRITE, target_type=TARGET_CANDIDATE, target_id=str(candidate_id)
        )
        try:
            decision = resolve.decide_candidate(
                session,
                org_id,
                workspace_id,
                candidate_id,
                body.decision == "accept",
                tenant.user_id,
            )
        except resolve.CandidateNotFound:
            raise not_found() from None
        except (resolve.AlreadyDecided, resolve.NotMergeable):
            raise api_error(409, CONFLICT) from None
        record_event(
            session,
            event_type=AuditEventType.GRAPH_MERGE_DECIDED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type=TARGET_CANDIDATE,
            target_id=str(candidate_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata={
                "workspace_id": str(workspace_id),
                "decision": decision.state,
                "winner_id": str(decision.winner_id) if decision.winner_id else None,
                "loser_id": str(decision.loser_id) if decision.loser_id else None,
            },
        )
    if decision.winner_id is not None and decision.loser_id is not None and graph_repo is not None:
        try:
            with tenant.transaction(org_id) as session:
                scope = validated_org_scope(session, org_id)
            graph_repo.remap_entity(scope, decision.loser_id, decision.winner_id)
        except (KnowledgeStoreError, UnknownOrganization):
            logger.warning("graph merge decided; projection remap deferred")
    return DecisionOut(id=candidate_id, state=decision.state, merged_into=decision.winner_id)
