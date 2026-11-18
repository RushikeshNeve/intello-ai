"""Version timeline, retention status and content URLs (M02.02.03, contract §5, §6).

**This module has no write endpoint on purpose**: versions are immutable (ADR-009). A new upload
is a new version (``finalize``); nothing here, or anywhere under ``/documents``, can change one. The
database backs this with a trigger that rejects every update except ``purged_at``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import ValidationError

from app.api.deps import INVALID_CURSOR, Tenant, TenantRequest, api_error, not_found
from app.core.models import INLINE_CONTENT_TYPES
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.documents.errors import storage_unavailable
from app.modules.documents.schemas import ContentUrl, Disposition, Version, VersionList
from app.modules.storage.deps import ObjectStoreDep
from app.modules.storage.ports import StorageUnavailable

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_DEFAULT_PRESIGN_TTL_SECONDS = 300


def get_presign_ttl() -> int:
    """``S3_PRESIGN_TTL_SECONDS`` (the object-store adapter's own setting; tests override it)."""
    from app.infrastructure.object_storage.settings import ObjectStoreSettings

    try:
        return ObjectStoreSettings().S3_PRESIGN_TTL_SECONDS
    except ValidationError:  # credentials not configured: the store itself will report 503
        return _DEFAULT_PRESIGN_TTL_SECONDS


def _base_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


@router.get("/documents/{document_id}/versions", response_model=VersionList)
def list_versions(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> VersionList:
    """Newest first; ``retention.status`` is ``current`` for the latest, else ``superseded``."""
    after: svc.PageCursor | None = None
    if cursor:
        try:
            after = svc.PageCursor.decode(cursor, svc.VERSION_SCOPE)
            svc.validate_version_cursor(after)
        except ValueError:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
        _row, _grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_READ)
        latest_id = svc.latest_version_id_of(session, ctx, document_id)
        items, next_cursor = svc.list_versions(
            session, ctx, document_id, latest_id, limit=limit, after=after
        )
    return VersionList(items=items, next_cursor=next_cursor)


def _load_version(
    tenant: TenantRequest,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> svc.VersionRow:
    with tenant.transaction(org_id) as session:
        ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.DOCUMENT_READ)
        _row, _grants = svc.authorize_document(session, ctx, document_id, Action.DOCUMENT_READ)
        latest_id = svc.latest_version_id_of(session, ctx, document_id)
        found = svc.fetch_version(session, ctx, document_id, version_id, latest_id)
    if found is None:
        raise not_found()
    return found


@router.get("/documents/{document_id}/versions/{version_id}", response_model=Version)
def get_version(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> Version:
    return _load_version(tenant, org_id, workspace_id, document_id, version_id).version


@router.get("/documents/{document_id}/versions/{version_id}/content-url", response_model=ContentUrl)
def get_content_url(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    store: ObjectStoreDep,
    ttl_seconds: Annotated[int, Depends(get_presign_ttl)],
    disposition: Disposition = "attachment",
) -> ContentUrl:
    """A short-lived presigned GET from the object-store origin (never the app origin).

    ``inline`` is honoured only for the allow-list in ``INLINE_CONTENT_TYPES`` (contract §6); any
    other type is forced to ``attachment`` and the response says so.
    """
    found = _load_version(tenant, org_id, workspace_id, document_id, version_id)
    if found.purged:
        raise not_found()
    version = found.version
    effective: Disposition = (
        "inline"
        if disposition == "inline" and _base_type(version.content_type) in INLINE_CONTENT_TYPES
        else "attachment"
    )
    # Taken before signing, so it never claims a longer life than the signature has.
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    try:
        url = store.presign_get(
            found.object_key,
            expires_seconds=ttl_seconds,
            filename=version.original_filename,
            content_type=version.content_type,
            inline=effective == "inline",
        )
    except StorageUnavailable:
        raise storage_unavailable() from None
    return ContentUrl(
        url=url,
        expires_at=expires_at,
        filename=version.original_filename,
        content_type=version.content_type,
        disposition=effective,
    )
