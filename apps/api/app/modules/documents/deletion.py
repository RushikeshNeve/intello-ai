"""Document deletion request (M02.02.04, contract m02-contracts.md §5).

``DELETE`` needs ``document.manage`` and is a **logical** deletion: ``deleted_at``/``deleted_by``
are set and from that commit the document and its versions are 404 for everyone (ADR-018). Objects
are not touched; the physical purge is a later job, due ``DOCUMENT_PURGE_GRACE_DAYS`` after.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import Tenant, not_found
from app.core.config import Settings, get_settings
from app.modules.audit import AuditEventType
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.documents.schemas import DeletionAccepted

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])


@router.delete(
    "/documents/{document_id}",
    response_model=DeletionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_document(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeletionAccepted:
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_MANAGE)
        row, _grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_MANAGE)
        deleted_at = svc.soft_delete_document(session, ctx, document_id, tenant.user_id)
        if deleted_at is None:  # a concurrent DELETE won
            raise not_found()
        purge_after = deleted_at + timedelta(days=settings.DOCUMENT_PURGE_GRACE_DAYS)
        svc.audit(
            tenant,
            session,
            AuditEventType.DOCUMENT_DELETED,
            ctx=ctx,
            document_id=document_id,
            metadata={"title": row.title, "purge_after": purge_after.isoformat()},
        )
    return DeletionAccepted(purge_after=purge_after)
