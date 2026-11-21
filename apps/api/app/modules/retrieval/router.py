"""Search API and the chunk-evidence endpoint (M04.01, contract m04 §3-§4).

* ``POST /v1/orgs/{org_id}/search`` grouped, highlighted results under the server-derived scope.
* ``GET  /v1/orgs/{org_id}/workspaces/{ws}/documents/{d}/versions/{v}/chunks/{chunk_id}`` chunk
  text from the artifact store (``document.read``; concealed 404 out of scope).

No query text reaches logs or audit rows (counts only).
"""

from __future__ import annotations

import math
import time
import uuid
from contextlib import AbstractContextManager

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, not_found
from app.modules.ai.embeddings.ports import EmbeddingProvider
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.documents.errors import storage_unavailable
from app.modules.knowledge.ports import UnknownOrganization
from app.modules.organizations.service import load_org_access
from app.modules.retrieval import service
from app.modules.retrieval.citations import ArtifactUnavailable, load_version_chunk
from app.modules.retrieval.hybrid import SearchUnavailable
from app.modules.retrieval.schemas import (
    ChunkEvidence,
    Neighbors,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchScopeInfo,
    Snippet,
)
from app.modules.retrieval.scope import EmptyScope, RetrievalScope, resolve_scope
from app.modules.retrieval.wiring import (
    ArtifactsDep,
    EmbedderDep,
    LimiterDep,
    RepoDep,
    SettingsDep,
    search_unavailable,
)
from app.modules.storage.ports import StorageUnavailable

router = APIRouter(prefix="/v1/orgs/{org_id}", tags=["search"])

RATE_LIMITED = "rate_limited"
EVIDENCE_UNAVAILABLE = "evidence_unavailable"
TARGET_SEARCH = "search"


def _metered(
    tenant: TenantRequest, org_id: uuid.UUID, embedder: EmbeddingProvider | None
) -> EmbeddingProvider | None:
    """Usage metering of the query embedding (M11.01); a no-op without an embedder."""
    from app.modules.usage.wiring import Meter

    def sessions(org: uuid.UUID) -> AbstractContextManager[Session]:
        return tenant.sessions(user_id=tenant.user_id, org_id=org)

    return Meter(
        sessions, org_id=org_id, user_id=tenant.user_id, enforce_embed_query=True
    ).embedder(embedder)


@router.post(
    "/search",
    response_model=SearchResponse,
    responses={
        404: {"description": "Organization not found or not a member (concealed)."},
        422: {"description": "Invalid request."},
        429: {"description": "Rate limited (60 requests/min/user); see Retry-After."},
        503: {"description": "The knowledge index is unavailable (never an empty result)."},
    },
)
def search(
    tenant: Tenant,
    org_id: uuid.UUID,
    body: SearchRequest,
    repo: RepoDep,
    embedder: EmbedderDep,
    limiter: LimiterDep,
    settings: SettingsDep,
) -> SearchResponse:
    wait = limiter.acquire(str(tenant.user_id))
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            RATE_LIMITED,
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    started = time.perf_counter()
    limit = body.limit or settings.SEARCH_DEFAULT_LIMIT

    scope: RetrievalScope | None
    with tenant.transaction(org_id) as session:
        access = load_org_access(session, org_id, tenant.user_id)
        if access is None:
            raise not_found()
        if access.role is None:
            access.require(Action.ORG_READ)  # audited, concealed 404
            raise not_found()
        try:
            scope = resolve_scope(
                session,
                org_id=org_id,
                user_id=tenant.user_id,
                requested_workspace_ids=body.workspace_ids,
            )
        except EmptyScope:
            scope = None
        except UnknownOrganization:
            raise not_found() from None

    gathered: service.Gathered | None = None
    if scope is not None:
        try:
            gathered = service.gather(
                scope,
                repo,
                _metered(tenant, org_id, embedder),
                body.query,
                candidates=max(
                    settings.SEARCH_CANDIDATES, limit * settings.SEARCH_MAX_PER_DOCUMENT
                ),
                mode=body.mode,
                max_per_document=settings.SEARCH_MAX_PER_DOCUMENT,
            )
        except SearchUnavailable:
            raise search_unavailable() from None
        except ValueError:  # the query is not searchable (e.g. only whitespace after cleanup)
            gathered = None

    with tenant.transaction(org_id) as session:
        results: list[service.ResultOut] = []
        degraded: tuple[str, ...] = ()
        dropped = 0
        candidate_count = 0
        evidence_count = 0
        if scope is not None and gathered is not None:
            outcome = service.finish(
                session,
                scope,
                gathered,
                top_k=limit * settings.SEARCH_MAX_PER_DOCUMENT,
                content_types=body.content_types,
            )
            results = service.group_results(
                outcome.evidence,
                body.query,
                limit=limit,
                max_snippets=settings.SEARCH_MAX_PER_DOCUMENT,
            )
            degraded = outcome.degraded
            dropped = outcome.dropped
            candidate_count = outcome.candidate_count
            evidence_count = len(outcome.evidence)
        took_ms = int((time.perf_counter() - started) * 1000)
        record_event(
            session,
            event_type=AuditEventType.SEARCH_PERFORMED,
            actor_user_id=tenant.user_id,
            org_id=org_id,
            target_type=TARGET_SEARCH,
            target_id=str(org_id),
            outcome="success",
            request_id=tenant.request_id,
            metadata={
                "mode": body.mode,
                "workspace_count": len(scope.workspace_ids) if scope is not None else 0,
                "candidates": candidate_count,
                "evidence": evidence_count,
                "dropped": dropped,
                "results": len(results),
                "degraded": list(degraded),
                "took_ms": took_ms,
            },
        )

    return SearchResponse(
        query=body.query,
        results=[
            SearchResult(
                document_id=r.document_id,
                title=r.title,
                workspace_id=r.workspace_id,
                workspace_slug=r.workspace_slug,
                content_type=r.content_type,
                version_id=r.version_id,
                version_number=r.version_number,
                score=r.score,
                snippets=[
                    Snippet(
                        evidence_id=s.evidence_id,
                        chunk_id=s.chunk_id,
                        text=s.text,
                        page_start=s.page_start,
                        page_end=s.page_end,
                        highlights=s.highlights,
                    )
                    for s in r.snippets
                ],
            )
            for r in results
        ],
        degraded=list(degraded),
        took_ms=took_ms,
        scope=SearchScopeInfo(workspace_count=len(scope.workspace_ids) if scope else 0),
    )


@router.get(
    "/workspaces/{workspace_id}/documents/{document_id}/versions/{version_id}/chunks/{chunk_id}",
    response_model=ChunkEvidence,
    responses={
        404: {"description": "Not found or out of scope (concealed)."},
        410: {"description": "The chunk artifact is missing or failed verification."},
        503: {"description": "The artifact store is unavailable."},
    },
)
def get_chunk_evidence(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    chunk_id: uuid.UUID,
    artifacts: ArtifactsDep,
) -> ChunkEvidence:
    """Chunk text of exactly this version, read from ``chunks.json`` (sha256-verified)."""
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
        row, _grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_READ)
        latest_id = svc.latest_version_id_of(session, ctx, document_id)
        found = svc.fetch_version(session, ctx, document_id, version_id, latest_id)
        if found is None or found.purged:
            raise not_found()
        try:
            view = load_version_chunk(
                session,
                artifacts,
                org_id=org_id,
                document_id=document_id,
                version_id=version_id,
                chunk_id=chunk_id,
            )
        except ArtifactUnavailable:
            raise HTTPException(status.HTTP_410_GONE, EVIDENCE_UNAVAILABLE) from None
        except StorageUnavailable:
            raise storage_unavailable() from None
        if view is None:
            raise not_found()
        return ChunkEvidence(
            chunk_id=view.chunk_id,
            text=view.text,
            page_start=view.page_start,
            page_end=view.page_end,
            heading_path=view.heading_path,
            locators=view.locators,
            version_number=found.version.version_number,
            title=row.title,
            neighbors=Neighbors(prev_chunk_id=view.prev_chunk_id, next_chunk_id=view.next_chunk_id),
        )
