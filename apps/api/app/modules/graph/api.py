"""Graph read API (M05.03.01, contract m05 §5): bounded neighborhood, entity search, claim detail.

All routes need ``document.read`` on the workspace (concealed 404 otherwise) and only ever see
that single workspace. Caps are enforced server-side; evidence is revalidated against PostgreSQL
and a claim without surviving evidence is never shown. No hidden counts are exposed.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import Tenant, not_found
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.graph.merge import resolve_evidence
from app.modules.graph.ports_read import GClaim
from app.modules.graph.read_settings import HARD_MAX_DEPTH, HARD_MAX_EDGES
from app.modules.graph.schemas import (
    ClaimDetailResponse,
    ClaimEntityOut,
    ClaimEvidenceOut,
    ClaimOut,
    EdgeOut,
    EntityListResponse,
    EntityOut,
    NeighborhoodResponse,
    NodeOut,
)
from app.modules.graph.traversal import traverse
from app.modules.graph.wiring import (
    ChunkSourceDep,
    GraphRepoDep,
    GraphSettingsDep,
    graph_unavailable,
)
from app.modules.knowledge.ports import UnknownOrganization
from app.modules.retrieval.scope import EmptyScope, RetrievalScope, resolve_scope
from app.modules.storage.ports import StorageUnavailable

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}/graph", tags=["graph"])

EXCERPT_CHARS = 280


def _encode(payload: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode(cursor: str | None) -> list[Any] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_cursor") from None
    if not isinstance(value, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_cursor")
    return value


def _scope(
    tenant: Tenant, session: Any, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> RetrievalScope:
    svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
    try:
        return resolve_scope(
            session,
            org_id=org_id,
            user_id=tenant.user_id,
            requested_workspace_ids=[workspace_id],
        )
    except (EmptyScope, UnknownOrganization):
        raise not_found() from None


@router.get(
    "/neighborhood",
    response_model=NeighborhoodResponse,
    responses={404: {"description": "Not found or out of scope (concealed)."}},
)
def neighborhood(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo: GraphRepoDep,
    chunks: ChunkSourceDep,
    settings: GraphSettingsDep,
    entity_id: uuid.UUID,
    depth: Annotated[int, Query(ge=1, le=HARD_MAX_DEPTH)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> NeighborhoodResponse:
    after = _decode(cursor)
    with tenant.transaction(org_id) as session:
        scope = _scope(tenant, session, org_id, workspace_id)
        try:
            sub = traverse(
                scope,
                repo,
                [entity_id],
                depth=min(depth, settings.GRAPH_TRAVERSAL_MAX_DEPTH),
                max_nodes=settings.GRAPH_MAX_NODES,
                max_edges=HARD_MAX_EDGES,
            )
            if entity_id not in sub.nodes:
                raise not_found()
            ordered = sorted(sub.edges, key=lambda c: (sub.hop[c.id], str(c.id)))
            if after is not None:
                try:
                    key = (int(after[0]), str(after[1]))
                except (ValueError, TypeError, IndexError):
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_cursor"
                    ) from None
                ordered = [c for c in ordered if (sub.hop[c.id], str(c.id)) > key]
            kept: list[tuple[GClaim, int]] = []
            index = 0
            while index < len(ordered) and len(kept) < limit:
                batch = ordered[index : index + limit]
                resolved = resolve_evidence(session, scope, batch, repo, chunks)
                for claim in batch:
                    index += 1
                    if resolved.get(claim.id):
                        kept.append((claim, len(resolved[claim.id])))
                        if len(kept) == limit:
                            break
        except StorageUnavailable:
            raise graph_unavailable() from None
    more = index < len(ordered)
    next_cursor = _encode([sub.hop[kept[-1][0].id], str(kept[-1][0].id)]) if more and kept else None
    ids = {entity_id} | {i for c, _n in kept for i in (c.subject_id, c.object_id) if i is not None}
    return NeighborhoodResponse(
        nodes=[
            NodeOut(
                id=i,
                type=sub.nodes[i].type,
                name=sub.nodes[i].canonical_name,
                degree_visible=sub.degree(i),
            )
            for i in sorted(ids, key=str)
            if i in sub.nodes
        ],
        edges=[
            EdgeOut(
                id=c.id,
                predicate=c.predicate,
                subject_id=c.subject_id,
                object_id=c.object_id,  # type: ignore[arg-type]  # entity edges only
                claim_id=c.id,
                confidence=c.confidence,
                evidence_count=n,
            )
            for c, n in kept
        ],
        truncated=sub.truncated or more,
        next_cursor=next_cursor,
    )


@router.get(
    "/entities",
    response_model=EntityListResponse,
    responses={404: {"description": "Not found or out of scope (concealed)."}},
)
def list_entities(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo: GraphRepoDep,
    q: Annotated[str, Query(max_length=200)] = "",
    type: Annotated[
        Literal[
            "person",
            "organization",
            "project",
            "technology",
            "concept",
            "location",
            "event",
            "document_ref",
            "other",
        ]
        | None,
        Query(),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> EntityListResponse:
    after_raw = _decode(cursor)
    after: tuple[str, uuid.UUID] | None = None
    if after_raw is not None:
        try:
            after = (str(after_raw[0]), uuid.UUID(str(after_raw[1])))
        except (ValueError, IndexError):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_cursor") from None
    with tenant.transaction(org_id) as session:
        scope = _scope(tenant, session, org_id, workspace_id)
        rows = [
            e
            for e in repo.search_entities(scope, q.strip(), type, limit + 1, after)
            if e.workspace_id == workspace_id and e.org_id == org_id and e.entity_state == "active"
        ]
    page = rows[:limit]
    more = len(rows) > limit
    return EntityListResponse(
        items=[EntityOut(id=e.id, type=e.type, name=e.canonical_name) for e in page],
        next_cursor=_encode([page[-1].canonical_name.casefold(), str(page[-1].id)])
        if more and page
        else None,
    )


@router.get(
    "/claims/{claim_id}",
    response_model=ClaimDetailResponse,
    responses={404: {"description": "Not found, unsupported or out of scope (concealed)."}},
)
def get_claim(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    claim_id: uuid.UUID,
    repo: GraphRepoDep,
    chunks: ChunkSourceDep,
) -> ClaimDetailResponse:
    with tenant.transaction(org_id) as session:
        scope = _scope(tenant, session, org_id, workspace_id)
        try:
            claim = repo.get_claim(scope, claim_id)
            if (
                claim is None
                or claim.workspace_id != workspace_id
                or claim.org_id != org_id
                or not claim.published
                or claim.verification != "verified"
            ):
                raise not_found()
            resolved = resolve_evidence(session, scope, [claim], repo, chunks).get(claim.id)
            if not resolved:
                raise not_found()  # a claim without live evidence is never shown
            wanted = [claim.subject_id] + ([claim.object_id] if claim.object_id else [])
            ents = {
                e.id: e
                for e in repo.get_entities(scope, wanted)
                if e.workspace_id == workspace_id and e.entity_state == "active"
            }
        except StorageUnavailable:
            raise graph_unavailable() from None
    subject = ents.get(claim.subject_id)
    if subject is None or (claim.object_id is not None and claim.object_id not in ents):
        raise not_found()
    obj = ents.get(claim.object_id) if claim.object_id else None
    return ClaimDetailResponse(
        claim=ClaimOut(
            id=claim.id,
            predicate=claim.predicate,
            subject=ClaimEntityOut(id=subject.id, type=subject.type, name=subject.canonical_name),
            object=ClaimEntityOut(id=obj.id, type=obj.type, name=obj.canonical_name)
            if obj
            else None,
            object_text=claim.object_text,
            confidence=claim.confidence,
        ),
        evidence=[
            ClaimEvidenceOut(
                chunk_id=gev.chunk_id,
                document_id=gev.document_id,
                version_id=gev.version_id,
                title=live.title,
                page_start=live.page_start,
                page_end=live.page_end,
                excerpt=(gev.excerpt or live.text)[:EXCERPT_CHARS],
            )
            for gev, live in resolved
        ],
    )
