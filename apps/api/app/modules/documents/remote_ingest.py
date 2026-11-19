"""Create a document / new version from bytes the platform fetched itself (M06 contract section 1).

What :mod:`app.modules.documents.finalize` does after verification, without a presigned upload: the
object is written server-side (conditional create, key derived from fresh ids), then the document
(or the next version of a live one), the immutable version row, the ingestion job + outbox row and
the audit event are written in the CALLER's transaction. Used by connectors (and URL capture), so a
fetched item takes exactly the path an upload takes: extraction, indexing, deletion and retrieval
rules are unchanged.

It reuses the row helpers of :mod:`upload_service` (``create_document``/``create_version``/
``next_version_number``) through a synthetic, already-finalized ``SessionRow``; M02 is untouched.
The caller holds no document lock: this function locks the document it versions.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.models import UPLOAD_STATE_FINALIZED, DocumentVersion
from app.modules.audit import AuditEventType, record_event
from app.modules.documents import upload_service
from app.modules.documents.schemas import default_title
from app.modules.ingestion.jobs import enqueue_ingestion
from app.modules.storage.keys import original_key
from app.modules.storage.ports import ObjectStore


class DocumentGone(Exception):
    """The document to version does not exist or was logically deleted."""


class QuotaExceeded(Exception):
    """Storing the bytes would exceed ``ORG_STORAGE_QUOTA_BYTES``."""


@dataclass(frozen=True)
class RemoteIngestResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    created: bool
    sha256: str


def stored_bytes(session: Session, org_id: uuid.UUID) -> int:
    value = session.execute(
        select(func.coalesce(func.sum(DocumentVersion.size_bytes), 0)).where(
            DocumentVersion.org_id == org_id, DocumentVersion.purged_at.is_(None)
        )
    ).scalar_one()
    return int(value)


def ingest_bytes(
    session: Session,
    store: ObjectStore,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    created_by: uuid.UUID,
    data: bytes,
    filename: str,
    content_type: str,
    title: str | None = None,
    document_id: uuid.UUID | None = None,
    request_id: str | None = None,
    audit_metadata: dict[str, object] | None = None,
) -> RemoteIngestResult:
    """New document (``document_id`` None) or the next version of ``document_id``."""
    settings = get_settings()
    if stored_bytes(session, org_id) + len(data) > settings.ORG_STORAGE_QUOTA_BYTES:
        raise QuotaExceeded
    digest = hashlib.sha256(data)
    sha = digest.hexdigest()
    version_id = uuid.uuid4()
    new_document = document_id is None
    doc_id = document_id or uuid.uuid4()
    version_number = 1
    if not new_document:
        row = upload_service.load_document(session, org_id, workspace_id, doc_id, lock=True)
        if row is None or row.deleted:
            raise DocumentGone
        version_number = upload_service.next_version_number(session, doc_id)
    key = original_key(org_id, doc_id, version_id)
    store.put_object(
        key,
        data,
        content_type=content_type,
        sha256_b64=base64.b64encode(digest.digest()).decode("ascii"),
    )
    synthetic = upload_service.SessionRow(
        id=uuid.uuid4(),
        org_id=org_id,
        workspace_id=workspace_id,
        document_id=doc_id,
        version_id=version_id,
        created_by=created_by,
        filename=filename,
        title=title,
        content_type=content_type,
        size_bytes=len(data),
        sha256=sha,
        object_key=key,
        state=UPLOAD_STATE_FINALIZED,
        failure_code=None,
        expired=False,
    )
    if new_document:
        upload_service.create_document(
            session, synthetic, source_id, title or default_title(filename)
        )
    upload_service.create_version(session, synthetic, version_number)
    enqueue_ingestion(
        session,
        org_id=org_id,
        workspace_id=workspace_id,
        document_id=doc_id,
        version_id=version_id,
        embedding_space_id=settings.embedding_space_id,
    )
    record_event(
        session,
        event_type=(
            AuditEventType.DOCUMENT_CREATED
            if new_document
            else AuditEventType.DOCUMENT_VERSION_CREATED
        ),
        actor_user_id=None,
        org_id=org_id,
        target_type=upload_service.TARGET_DOCUMENT,
        target_id=str(doc_id),
        outcome="success",
        request_id=request_id,
        metadata={
            "workspace_id": str(workspace_id),
            "document_id": str(doc_id),
            "version_id": str(version_id),
            "version_number": version_number,
            "source_id": str(source_id),
            "size_bytes": len(data),
            "content_type": content_type,
            **(audit_metadata or {}),
        },
    )
    return RemoteIngestResult(doc_id, version_id, version_number, new_document, sha)
