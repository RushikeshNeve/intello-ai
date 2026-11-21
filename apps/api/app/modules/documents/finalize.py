"""Finalize an upload (M02.01.04, contract m02-contracts.md section 5, steps 1-4).

``POST {ws}/uploads/{upload_id}/finalize``: the creator of a session only, re-authorized now
(``document.write``). The session row is locked ``FOR UPDATE`` for the whole request, so concurrent
finalizes of one session serialize and the loser gets the idempotent 200. The object is verified
against what the session declared (existence, size, SHA-256 stored by the store); only then is the
version created, in the same transaction as the document/source, the session state and the audit
event. An object nobody declared is never adopted: keys derive only from sessions.

A verification failure must outlive the error response, so it is recorded (and the object deleted)
inside the transaction, which is *committed*, and the HTTP error is raised after the commit.

Interpretations of the contract (reported in the handoff): finalizing a ``failed`` session again
repeats its 409 (``size_mismatch`` / ``checksum_mismatch``), an ``expired`` one repeats 410; a
session finalize that finds its document deleted answers 404 and leaves the session ``pending``.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, not_found
from app.core.config import get_settings
from app.core.models import (
    UPLOAD_STATE_EXPIRED,
    UPLOAD_STATE_FAILED,
    UPLOAD_STATE_FINALIZED,
    UPLOAD_STATE_PENDING,
)
from app.modules.audit import AuditEventType, record_denied, record_event
from app.modules.authorization.policy import Action
from app.modules.documents import errors, upload_service
from app.modules.documents.schemas import FinalizeResult, default_title, sha256_hex_to_b64
from app.modules.ingestion.jobs import enqueue_ingestion
from app.modules.retrieval.cache import bump_corpus
from app.modules.storage.deps import ObjectStoreDep
from app.modules.storage.ports import StorageError

logger = logging.getLogger("ukos.documents.finalize")

# Mounted on ``upload.router``, which owns
# the ``/v1/orgs/{org_id}/workspaces/{workspace_id}`` prefix.
router = APIRouter(tags=["documents"])


@router.post(
    "/uploads/{upload_id}/finalize",
    response_model=FinalizeResult,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": FinalizeResult, "description": "Already finalized"}},
)
def finalize_upload(
    tenant: Tenant,
    store: ObjectStoreDep,
    response: Response,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> FinalizeResult:
    """Verify the uploaded object and create the document version."""
    failure: HTTPException | None = None
    result: FinalizeResult | None = None
    repeat = False
    with tenant.transaction(org_id) as session:
        sess = upload_service.load_session(session, org_id, upload_id)
        if sess is None or sess.workspace_id != workspace_id or sess.created_by != tenant.user_id:
            # Non-members get the audited 404 from the authorization step; everyone else a 404.
            upload_service.authorize_write(
                session,
                tenant.user_id,
                org_id,
                workspace_id,
                document_id=None,
                target_type=upload_service.TARGET_UPLOAD_SESSION,
                target_id=str(upload_id),
            )
            if sess is not None and sess.workspace_id == workspace_id:
                record_denied(
                    tenant.sessions,
                    actor_user_id=tenant.user_id,
                    org_id=org_id,
                    target_type=upload_service.TARGET_UPLOAD_SESSION,
                    target_id=str(upload_id),
                    request_id=tenant.request_id,
                    metadata={
                        "action": Action.DOCUMENT_WRITE.value,
                        "reason": upload_service.REASON_NOT_UPLOAD_CREATOR,
                    },
                )
            raise not_found()

        access = upload_service.authorize_write(
            session,
            tenant.user_id,
            org_id,
            workspace_id,
            document_id=sess.document_id,
            target_type=upload_service.TARGET_UPLOAD_SESSION,
            target_id=str(upload_id),
        )
        # Step 1: serialize on the session row (re-read: it may have changed while we waited).
        locked = upload_service.load_session(session, org_id, upload_id, lock=True)
        assert locked is not None  # noqa: S101 - the row was just read and rows are never deleted
        sess = locked

        if sess.state == UPLOAD_STATE_FINALIZED:
            repeat = True
            result = upload_service.build_result(
                session,
                sess,
                upload_service.refreshed_subject(
                    session, access, tenant.user_id, workspace_id, sess.document_id
                ),
            )
        elif sess.state == UPLOAD_STATE_FAILED:
            raise upload_service.failure_http(sess.failure_code or errors.CHECKSUM_MISMATCH)
        elif sess.state == UPLOAD_STATE_EXPIRED:
            raise errors.upload_expired()
        elif sess.expired:
            # Step 2: lapsed. Record it (committed below), then answer 410.
            upload_service.mark_session(session, upload_id, UPLOAD_STATE_EXPIRED, None)
            failure = errors.upload_expired()
        else:
            assert sess.state == UPLOAD_STATE_PENDING  # noqa: S101 - closed set of states
            # Step 3: the object must exist and be exactly what was declared.
            try:
                info = store.head_object(sess.object_key)
            except StorageError as exc:
                raise upload_service.storage_error_to_http(exc) from None
            if info is None:
                raise errors.object_missing()  # session stays pending: re-PUT and retry
            code: str | None = None
            if info.size != sess.size_bytes:
                code = errors.FAILURE_SIZE_MISMATCH
            elif info.checksum_sha256_b64 != sha256_hex_to_b64(sess.sha256):
                code = errors.FAILURE_CHECKSUM_MISMATCH  # includes "store kept no checksum"
            if code is not None:
                try:
                    store.delete_object(sess.object_key)
                except StorageError as exc:
                    # Never mark a session failed while its bad object may still be there.
                    raise upload_service.storage_error_to_http(exc) from None
                upload_service.mark_session(session, upload_id, UPLOAD_STATE_FAILED, code)
                logger.warning(
                    "upload verification failed",
                    extra={"upload_id": str(upload_id), "org_id": str(org_id), "code": code},
                )
                failure = upload_service.failure_http(code)
            else:
                # Step 4: one transaction creates everything and marks the session finalized.
                result = _create_version(session, tenant, sess, org_id, workspace_id, access)

    if failure is not None:
        raise failure
    assert result is not None  # noqa: S101 - every non-failure branch sets it
    if repeat:
        response.status_code = status.HTTP_200_OK
    else:
        bump_corpus(org_id)  # M11.04: a new document/version invalidates cached retrievals
    return result


def _create_version(
    session: Session,
    tenant: TenantRequest,
    sess: upload_service.SessionRow,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    access: upload_service.WriteAccess,
) -> FinalizeResult:
    source_id = upload_service.get_or_create_upload_source(
        session, org_id, workspace_id, tenant.user_id
    )
    # New document: the reserved id has no row yet. Otherwise lock the live document so version
    # numbers are assigned one at a time; a deletion that happened meanwhile is a 404.
    document = upload_service.load_document(
        session, org_id, workspace_id, sess.document_id, lock=True
    )
    if document is not None and document.deleted:
        raise not_found()
    if document is None:
        upload_service.create_document(
            session, sess, source_id, sess.title or default_title(sess.filename)
        )
        event = AuditEventType.DOCUMENT_CREATED
        version_number = 1
    else:
        event = AuditEventType.DOCUMENT_VERSION_CREATED
        version_number = upload_service.next_version_number(session, sess.document_id)
    upload_service.create_version(session, sess, version_number)
    # M03 contract §2: the ingestion job + its queued event + the outbox row are written in THIS
    # transaction, so a rollback leaves none of them (transactional outbox). Only this branch runs
    # for a new version: an idempotent repeat of a finalized session never reaches it.
    enqueue_ingestion(
        session,
        org_id=org_id,
        workspace_id=workspace_id,
        document_id=sess.document_id,
        version_id=sess.version_id,
        embedding_space_id=get_settings().embedding_space_id,
    )
    upload_service.mark_session(session, sess.id, UPLOAD_STATE_FINALIZED, None)
    record_event(
        session,
        event_type=event,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=upload_service.TARGET_DOCUMENT,
        target_id=str(sess.document_id),
        outcome="success",
        request_id=tenant.request_id,
        # Ids, size and type only: never the filename, the object key or any URL.
        metadata={
            "workspace_id": str(workspace_id),
            "document_id": str(sess.document_id),
            "version_id": str(sess.version_id),
            "version_number": version_number,
            "upload_id": str(sess.id),
            "size_bytes": sess.size_bytes,
            "content_type": sess.content_type,
        },
    )
    subject = upload_service.refreshed_subject(
        session, access, tenant.user_id, workspace_id, sess.document_id
    )
    return upload_service.build_result(session, sess, subject)
