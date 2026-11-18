"""Upload sessions (M02.01.03, contract m02-contracts.md section 5) and
(via ``finalize``) M02.01.04.

``POST {ws}/uploads`` order of checks: authentication -> authorization (``document.write``, audited
404/403) -> body validation (422) -> quotas (413, 429, 409) -> the session is stored and the
presigned PUT is returned. An unauthorized caller never learns anything about the body rules or the
quotas. The routes of ``finalize.py`` are mounted on this router (``router.py`` includes only it).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.deps import Tenant, not_found
from app.core.config import Settings, get_settings
from app.modules.documents import errors, finalize, upload_service
from app.modules.documents.schemas import (
    PresignedUpload,
    UploadCreate,
    UploadLimits,
    UploadSession,
    sha256_hex_to_b64,
)
from app.modules.storage.deps import ObjectStoreDep
from app.modules.storage.keys import original_key
from app.modules.storage.ports import StorageError

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])
router.include_router(finalize.router)


def _parse_body(raw: dict[str, Any]) -> UploadCreate:
    """Validate after authorization; same 422 shape as FastAPI's own body validation."""
    try:
        return UploadCreate.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in exc.errors()]
        ) from None


@router.post(
    "/uploads",
    response_model=UploadSession,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": UploadCreate.model_json_schema()}},
        }
    },
)
def create_upload(
    tenant: Tenant,
    store: ObjectStoreDep,
    settings: Annotated[Settings, Depends(get_settings)],
    ttl_seconds: Annotated[int, Depends(upload_service.get_presign_ttl_seconds)],
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    raw: Annotated[dict[str, Any], Body()],
) -> UploadSession:
    """Issue an upload session: a presigned PUT bound to the declared type and SHA-256."""
    claimed = raw.get("document_id")
    claimed_id: uuid.UUID | None = None
    if claimed is not None:
        try:
            claimed_id = uuid.UUID(str(claimed))
        except ValueError:
            claimed_id = (
                None  # malformed: authorized as a new document, then rejected by validation
            )
    with tenant.transaction(org_id) as session:
        access = upload_service.authorize_write(
            session,
            tenant.user_id,
            org_id,
            workspace_id,
            document_id=claimed_id,
            target_type=upload_service.TARGET_DOCUMENT if claimed_id else "workspace",
            target_id=str(claimed_id or workspace_id),
        )
        body = _parse_body(raw)
        if body.document_id is not None and (access.document is None or access.document.deleted):
            raise not_found()

        if body.size_bytes > settings.UPLOAD_MAX_BYTES:
            raise errors.file_too_large()
        upload_service.lock_org_uploads(session, org_id)
        if (
            upload_service.pending_count(session, org_id, tenant.user_id)
            >= settings.UPLOAD_MAX_PENDING_PER_USER
        ):
            raise errors.too_many_uploads()
        usage = upload_service.org_usage_bytes(session, org_id)
        if usage + body.size_bytes > settings.ORG_STORAGE_QUOTA_BYTES:
            raise errors.quota_exceeded()

        document_id = body.document_id or uuid.uuid4()
        version_id = uuid.uuid4()
        upload_id = uuid.uuid4()
        object_key = original_key(org_id, document_id, version_id)
        try:
            presigned = store.presign_put(
                object_key,
                content_type=body.content_type,
                sha256_b64=sha256_hex_to_b64(body.sha256),
                expires_seconds=ttl_seconds,
            )
        except StorageError as exc:
            raise upload_service.storage_error_to_http(exc) from None
        expires_at = upload_service.insert_session(
            session,
            session_id=upload_id,
            org_id=org_id,
            workspace_id=workspace_id,
            document_id=document_id,
            version_id=version_id,
            user_id=tenant.user_id,
            filename=body.filename,
            # Only meaningful for a NEW document; a version upload keeps the document's own title.
            title=body.title if body.document_id is None else None,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            sha256=body.sha256,
            object_key=object_key,
            ttl_seconds=settings.UPLOAD_SESSION_TTL_SECONDS,
        )
    return UploadSession(
        upload_id=upload_id,
        document_id=document_id,
        version_id=version_id,
        expires_at=expires_at,
        upload=PresignedUpload(method="PUT", url=presigned.url, headers=dict(presigned.headers)),
        limits=UploadLimits(max_bytes=settings.UPLOAD_MAX_BYTES),
    )
