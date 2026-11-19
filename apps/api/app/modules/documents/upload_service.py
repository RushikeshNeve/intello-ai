"""Private helpers of the upload endpoints (M02.01.03, M02.01.04).

Every function takes a tenant session.

Contract: ``docs/architecture/m02-contracts.md`` section 5 (Uploads). Used only by ``upload.py`` and
``finalize.py``.

Interpretations of the contract (reported in the M02.01.03/04 handoff):

* **Pending cap and quota use non-expired sessions only** (``state = 'pending' AND expires_at >
  clock_timestamp()``): nothing flips a lapsed ``pending`` row to ``expired`` until somebody
  finalizes it, so counting it forever would lock a user out after 20 abandoned uploads.
* Races on the pending cap and the quota are closed with a transaction-scoped advisory lock keyed
  on the org (held until the session row is committed), not with a lock on the organization row.
* All "now" comparisons use the database clock (``clock_timestamp()``), never the API host's.
* The optional ``title`` of ``POST uploads`` is kept on the session (``upload_sessions.title``,
  migration 0004, DC-022) and becomes the new document's title at finalize; without one the title is
  the filename without its extension.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from fastapi import HTTPException
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import not_found
from app.core.models import (
    SOURCE_KIND_UPLOAD,
    UPLOAD_STATE_FINALIZED,
    UPLOAD_STATE_PENDING,
    Document,
    DocumentVersion,
    Source,
    UploadSession,
)
from app.modules.authorization.grants import ResourceRef, active_permissions
from app.modules.authorization.policy import (
    Action,
    GrantPermission,
    SubjectContext,
    WorkspaceRole,
    decide,
)
from app.modules.documents import errors
from app.modules.documents.schemas import (
    Document as DocumentOut,
)
from app.modules.documents.schemas import (
    DocumentPermissions,
    FinalizeResult,
    Retention,
)
from app.modules.documents.schemas import (
    Version as VersionOut,
)
from app.modules.ingestion.status_service import latest_job, summarize
from app.modules.organizations.service import OrgAccess, load_org_access
from app.modules.storage.ports import StorageError
from app.modules.workspaces import service as workspace_service
from app.modules.workspaces.service import WorkspaceRow

TARGET_DOCUMENT: Final = "document"
TARGET_UPLOAD_SESSION: Final = "upload_session"
REASON_NOT_UPLOAD_CREATOR: Final = "not_upload_creator"
UPLOAD_SOURCE_NAME: Final = "Uploads"


# --- settings ------------------------------------------------------------------------------


class _PresignSettings(BaseSettings):
    """Only ``S3_PRESIGN_TTL_SECONDS`` (the store's own setting), without needing credentials."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    S3_PRESIGN_TTL_SECONDS: int = Field(default=300, ge=1, le=7 * 24 * 3600)


def get_presign_ttl_seconds() -> int:
    """FastAPI dependency: lifetime of a presigned upload URL (``S3_PRESIGN_TTL_SECONDS``)."""
    return _PresignSettings().S3_PRESIGN_TTL_SECONDS


def storage_error_to_http(exc: StorageError) -> HTTPException:
    """Every object-store failure is a 503 ``storage_unavailable`` (no detail is leaked)."""
    del exc
    return errors.storage_unavailable()


# --- access --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DocRow:
    id: uuid.UUID
    title: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    latest_version_id: uuid.UUID | None
    deleted: bool


@dataclass(frozen=True)
class WriteAccess:
    """The caller's ``document.write`` decision context for one upload/finalize request."""

    access: OrgAccess  # its subject carries the workspace role and the document grants
    workspace: WorkspaceRow
    document: DocRow | None  # the existing document (live or deleted), if the id names one


def load_document(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    lock: bool = False,
) -> DocRow | None:
    stmt = select(
        Document.id,
        Document.title,
        Document.created_by,
        Document.created_at,
        Document.updated_at,
        Document.latest_version_id,
        Document.deleted_at,
    ).where(
        Document.id == document_id,
        Document.org_id == org_id,
        Document.workspace_id == workspace_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    row = session.execute(stmt).one_or_none()
    if row is None:
        return None
    return DocRow(
        id=row.id,
        title=row.title,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        latest_version_id=row.latest_version_id,
        deleted=row.deleted_at is not None,
    )


def authorize_write(
    session: Session,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    document_id: uuid.UUID | None,
    target_type: str,
    target_id: str,
) -> WriteAccess:
    """``document.write`` for the caller; raises the audited 404/403 (:class:`AuthzDenied`).

    Order: the org (missing -> 404) -> org membership (audited 404) -> the workspace (404) -> the
    decision. With ``document_id`` naming a live document of this workspace the decision includes
    that document's active grants, so a member with a ``write`` grant but no workspace role may
    upload a new version. A deleted or unknown document id never helps the caller: the decision is
    taken without grants first (an unauthorized caller learns nothing), then it is a plain 404.
    """
    org = load_org_access(session, org_id, user_id)
    if org is None:
        raise not_found()
    if org.role is None:
        org.require(Action.DOCUMENT_WRITE, target_type=target_type, target_id=target_id)
    found = workspace_service.load_workspace(session, org_id, workspace_id, user_id)
    if found is None:
        raise not_found()
    workspace, role = found
    subject = replace(org.subject, workspace_role=role)
    document = (
        None if document_id is None else load_document(session, org_id, workspace_id, document_id)
    )
    if document is not None and not document.deleted:
        subject = replace(
            subject, grants=_grants(session, org_id, workspace_id, document.id, user_id, role)
        )
    access = replace(org, subject=subject)
    access.require(Action.DOCUMENT_WRITE, target_type=target_type, target_id=target_id)
    return WriteAccess(access=access, workspace=workspace, document=document)


def _grants(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    role: WorkspaceRole | None,
) -> frozenset[GrantPermission]:
    return active_permissions(
        session,
        resource=ResourceRef(org_id=org_id, workspace_id=workspace_id, resource_id=document_id),
        user_id=user_id,
        workspace_role=role,
    )


# --- quotas --------------------------------------------------------------------------------


def lock_org_uploads(session: Session, org_id: uuid.UUID) -> None:
    """Serialize upload-session creation per org until the transaction ends."""
    key = f"upload_quota|{org_id}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


def pending_count(session: Session, org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    count = session.execute(
        select(func.count())
        .select_from(UploadSession)
        .where(
            UploadSession.org_id == org_id,
            UploadSession.created_by == user_id,
            UploadSession.state == UPLOAD_STATE_PENDING,
            UploadSession.expires_at > func.clock_timestamp(),
        )
    ).scalar_one()
    return int(count)


def org_usage_bytes(session: Session, org_id: uuid.UUID) -> int:
    """Unpurged version bytes plus the bytes of the org's non-expired pending sessions."""
    stored = session.execute(
        select(func.coalesce(func.sum(DocumentVersion.size_bytes), 0)).where(
            DocumentVersion.org_id == org_id, DocumentVersion.purged_at.is_(None)
        )
    ).scalar_one()
    pending = session.execute(
        select(func.coalesce(func.sum(UploadSession.size_bytes), 0)).where(
            UploadSession.org_id == org_id,
            UploadSession.state == UPLOAD_STATE_PENDING,
            UploadSession.expires_at > func.clock_timestamp(),
        )
    ).scalar_one()
    return int(stored) + int(pending)


# --- session creation ----------------------------------------------------------------------


@dataclass(frozen=True)
class NewSession:
    id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    object_key: str


def insert_session(
    session: Session,
    *,
    session_id: uuid.UUID,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str,
    title: str | None,
    content_type: str,
    size_bytes: int,
    sha256: str,
    object_key: str,
    ttl_seconds: int,
) -> datetime:
    """Insert a ``pending`` session; return its (database-clock) ``expires_at``."""
    expires_at = session.execute(
        insert(UploadSession)
        .values(
            id=session_id,
            org_id=org_id,
            workspace_id=workspace_id,
            document_id=document_id,
            version_id=version_id,
            created_by=user_id,
            filename=filename,
            title=title,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            object_key=object_key,
            state=UPLOAD_STATE_PENDING,
            expires_at=func.now() + timedelta(seconds=ttl_seconds),
        )
        .returning(UploadSession.expires_at)
    ).scalar_one()
    assert isinstance(expires_at, datetime)  # noqa: S101 - typing narrowing for mypy
    return expires_at


# --- finalize ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    created_by: uuid.UUID
    filename: str
    title: str | None  # the optional title requested for a new document
    content_type: str
    size_bytes: int
    sha256: str
    object_key: str
    state: str
    failure_code: str | None
    expired: bool  # the database clock is past expires_at


def load_session(
    session: Session, org_id: uuid.UUID, upload_id: uuid.UUID, *, lock: bool = False
) -> SessionRow | None:
    stmt = select(
        UploadSession.id,
        UploadSession.org_id,
        UploadSession.workspace_id,
        UploadSession.document_id,
        UploadSession.version_id,
        UploadSession.created_by,
        UploadSession.filename,
        UploadSession.title,
        UploadSession.content_type,
        UploadSession.size_bytes,
        UploadSession.sha256,
        UploadSession.object_key,
        UploadSession.state,
        UploadSession.failure_code,
        (func.clock_timestamp() > UploadSession.expires_at).label("expired"),
    ).where(UploadSession.id == upload_id, UploadSession.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update()
    row = session.execute(stmt).one_or_none()
    if row is None:
        return None
    return SessionRow(
        id=row.id,
        org_id=row.org_id,
        workspace_id=row.workspace_id,
        document_id=row.document_id,
        version_id=row.version_id,
        created_by=row.created_by,
        filename=row.filename,
        title=row.title,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        object_key=row.object_key,
        state=row.state,
        failure_code=row.failure_code,
        expired=bool(row.expired),
    )


def mark_session(
    session: Session, upload_id: uuid.UUID, state: str, failure_code: str | None
) -> None:
    """Move a ``pending`` session forward (``failed`` needs a code)."""
    values: dict[str, object] = {"state": state, "failure_code": failure_code}
    if state == UPLOAD_STATE_FINALIZED:
        values["finalized_at"] = func.clock_timestamp()
    session.execute(
        update(UploadSession)
        .where(UploadSession.id == upload_id, UploadSession.state == UPLOAD_STATE_PENDING)
        .values(**values)
    )


def failure_http(code: str) -> HTTPException:
    """The HTTP error of a ``failed`` session (``failure_code`` is the 409 code)."""
    if code == errors.SIZE_MISMATCH:
        return errors.size_mismatch()
    return errors.checksum_mismatch()


def get_or_create_upload_source(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    """The workspace's single ``upload`` source; created lazily, safe against the race."""
    where = (Source.workspace_id == workspace_id, Source.kind == SOURCE_KIND_UPLOAD)
    found = session.execute(select(Source.id).where(*where)).scalar_one_or_none()
    if found is not None:
        return found
    session.execute(
        pg_insert(Source)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            workspace_id=workspace_id,
            kind=SOURCE_KIND_UPLOAD,
            name=UPLOAD_SOURCE_NAME,
            created_by=user_id,
        )
        .on_conflict_do_nothing(
            index_elements=[Source.workspace_id],
            # A LITERAL, not a bound parameter: once psycopg server-side-prepares this statement
            # (5th execution on a connection) PostgreSQL cannot match a parameterized predicate to
            # the partial unique index and fails with InvalidColumnReference.
            index_where=Source.kind == literal_column(f"'{SOURCE_KIND_UPLOAD}'"),
        )
    )
    # READ COMMITTED: a concurrent creator's committed row is visible to this new statement.
    return session.execute(select(Source.id).where(*where)).scalar_one()


def next_version_number(session: Session, document_id: uuid.UUID) -> int:
    """``max + 1``; call only while holding the document row lock."""
    current = session.execute(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0)).where(
            DocumentVersion.document_id == document_id
        )
    ).scalar_one()
    return int(current) + 1


def create_document(session: Session, sess: SessionRow, source_id: uuid.UUID, title: str) -> None:
    session.execute(
        insert(Document).values(
            id=sess.document_id,
            org_id=sess.org_id,
            workspace_id=sess.workspace_id,
            source_id=source_id,
            title=title,
            created_by=sess.created_by,
        )
    )


def create_version(session: Session, sess: SessionRow, version_number: int) -> None:
    session.execute(
        insert(DocumentVersion).values(
            id=sess.version_id,
            org_id=sess.org_id,
            workspace_id=sess.workspace_id,
            document_id=sess.document_id,
            version_number=version_number,
            original_filename=sess.filename,
            content_type=sess.content_type,
            size_bytes=sess.size_bytes,
            sha256=sess.sha256,
            object_key=sess.object_key,
            created_by=sess.created_by,
        )
    )
    session.execute(
        update(Document)
        .where(Document.id == sess.document_id, Document.org_id == sess.org_id)
        .values(latest_version_id=sess.version_id, updated_at=func.clock_timestamp())
    )


def build_result(session: Session, sess: SessionRow, subject: SubjectContext) -> FinalizeResult:
    """``FinalizeResult`` for a finalized session (also the idempotent repeat)."""
    doc = load_document(session, sess.org_id, sess.workspace_id, sess.document_id)
    if doc is None or doc.deleted:
        raise not_found()
    wanted = [sess.version_id] + ([doc.latest_version_id] if doc.latest_version_id else [])
    rows = session.execute(
        select(
            DocumentVersion.id,
            DocumentVersion.document_id,
            DocumentVersion.version_number,
            DocumentVersion.original_filename,
            DocumentVersion.content_type,
            DocumentVersion.size_bytes,
            DocumentVersion.sha256,
            DocumentVersion.created_by,
            DocumentVersion.created_at,
        ).where(
            DocumentVersion.document_id == doc.id,
            DocumentVersion.id.in_(wanted),
        )
    ).all()
    by_id = {row.id: row for row in rows}
    count = session.execute(
        select(func.count())
        .select_from(DocumentVersion)
        .where(DocumentVersion.document_id == doc.id)
    ).scalar_one()

    def version(row_id: uuid.UUID) -> VersionOut:
        row = by_id[row_id]
        return VersionOut(
            id=row.id,
            document_id=row.document_id,
            version_number=row.version_number,
            original_filename=row.original_filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            created_by=row.created_by,
            created_at=row.created_at,
            retention=Retention(
                status="current" if row.id == doc.latest_version_id else "superseded"
            ),
        )

    return FinalizeResult(
        document=DocumentOut(
            id=doc.id,
            org_id=sess.org_id,
            workspace_id=sess.workspace_id,
            title=doc.title,
            created_by=doc.created_by,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            version_count=int(count),
            latest_version=None
            if doc.latest_version_id is None
            else version(doc.latest_version_id),
            permissions=DocumentPermissions(
                write=decide(subject, Action.DOCUMENT_WRITE).allowed,
                manage=decide(subject, Action.DOCUMENT_MANAGE).allowed,
            ),
            # The job was enqueued in this same transaction: report it (M03.03.04).
            ingestion=summarize(
                latest_job(session, sess.org_id, sess.workspace_id, doc.id),
                can_write=decide(subject, Action.DOCUMENT_WRITE).allowed,
            ),
        ),
        version=version(sess.version_id),
    )


def refreshed_subject(
    session: Session,
    access: WriteAccess,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
) -> SubjectContext:
    """The subject with the document's grants (they may not have existed at authorization time)."""
    subject = access.access.subject
    return replace(
        subject,
        grants=_grants(
            session,
            access.workspace.org_id,
            workspace_id,
            document_id,
            user_id,
            subject.workspace_role,
        ),
    )
