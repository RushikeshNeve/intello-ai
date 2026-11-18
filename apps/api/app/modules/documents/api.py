"""Document list, get and rename (M02.02.02, contract m02-contracts.md §5).

``GET`` list/get need ``document.read`` (a read grant alone shows that document to a user without
a workspace role), ``PATCH`` needs ``document.write``. Deleted documents are 404 for everyone.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import INVALID_CURSOR, Tenant, api_error, not_found
from app.modules.audit import AuditEventType
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.documents.schemas import (
    Document,
    DocumentList,
    DocumentSort,
    DocumentUpdate,
    SortOrder,
)

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _invalid_cursor() -> HTTPException:
    return api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR)


@router.get("/documents", response_model=DocumentList)
def list_documents(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    q: Annotated[str | None, Query(max_length=255)] = None,
    content_type: Annotated[str | None, Query(max_length=255)] = None,
    sort: DocumentSort = "updated_at",
    order: SortOrder | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> DocumentList:
    """Live documents the caller may read. ``order`` defaults to asc for title, else desc."""
    effective_order: SortOrder = order or ("asc" if sort == "title" else "desc")
    after: svc.PageCursor | None = None
    if cursor:
        try:
            after = svc.PageCursor.decode(cursor, svc.document_scope(sort, effective_order))
            svc.validate_cursor_key(sort, after)
        except ValueError:
            raise _invalid_cursor() from None
    filters = svc.ListFilters(
        q=(q or "").strip() or None,
        content_type=(content_type or "").strip().lower() or None,
        sort=sort,
        order=effective_order,
        limit=limit,
        after=after,
    )
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
        if not ctx.sees_every_document and not svc.has_visible_document(session, ctx):
            # An org member with no workspace role and no grant: audited 404 not_workspace_member.
            ctx.require(
                Action.DOCUMENT_READ, target_type=svc.TARGET_WORKSPACE, target_id=str(workspace_id)
            )
        rows, next_cursor = svc.list_documents(session, ctx, filters)
        grants = svc.grants_by_document(session, ctx, [r.id for r in rows])
        items = [svc.document_out(ctx, r, grants.get(r.id, frozenset())) for r in rows]
        can_upload = ctx.allowed(Action.DOCUMENT_WRITE)
    return DocumentList(items=items, next_cursor=next_cursor, can_upload=can_upload)


@router.get("/documents/{document_id}", response_model=Document)
def get_document(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
        row, grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_READ)
        return svc.document_out(ctx, row, grants)


@router.patch("/documents/{document_id}", response_model=Document)
def update_document(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    body: DocumentUpdate,
) -> Document:
    """Rename. No change -> 200 with neither an update nor an audit row."""
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_WRITE)
        row, grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_WRITE)
        current = svc.lock_document(session, ctx, document_id)
        if current is None:  # deleted while we waited for the lock
            raise not_found()
        if body.title != current:
            svc.rename_document(session, ctx, document_id, body.title)
            svc.audit(
                tenant,
                session,
                AuditEventType.DOCUMENT_UPDATED,
                ctx=ctx,
                document_id=document_id,
                metadata={"from_title": current, "to_title": body.title},
            )
            reloaded = svc.fetch_document(session, ctx, document_id)
            if reloaded is None:
                raise not_found()
            row = reloaded
        return svc.document_out(ctx, row, grants)
