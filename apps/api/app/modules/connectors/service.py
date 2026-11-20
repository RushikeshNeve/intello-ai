"""Source management (M06.01.01/.04/.06): create, read, request a sync, revoke.

Shared by the routes, the webhook and the tests. Everything here runs in the CALLER's tenant
transaction. Revocation (contract m06 principle 4) is a single transaction: the source row is
locked and set ``revoked`` (a running sync's per-item ``FOR SHARE`` check then fails), the account
token and the webhook secret are wiped, queued syncs are cancelled, every document the source
brought in is logically deleted (the existing 404 paths of M02/M03/M04 apply from that commit) and
a purge event is enqueued per document.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any

from fastapi import status
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import TenantRequest, api_error, not_found
from app.core.config import Settings
from app.core.models import Document, Source
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.connectors import crypto
from app.modules.connectors.github import GitHubConnector
from app.modules.connectors.models import (
    AGGREGATE_SOURCE,
    EVENT_SOURCE_SYNC_REQUESTED,
    ITEM_ACTIVE,
    ITEM_DELETED,
    PROVIDER_GITHUB,
    SOURCE_STATE_ACTIVE,
    SOURCE_STATE_REVOKED,
    SYNC_CANCELLED,
    SYNC_QUEUED,
    ConnectorAccount,
    SourceItem,
    SourceSync,
)
from app.modules.connectors.schemas import (
    CreateSource,
    ItemCounts,
    SourceCreated,
    SourceOut,
    SyncSummary,
)
from app.modules.documents import catalog_service as svc
from app.modules.ingestion.jobs import enqueue_document_deleted
from app.modules.ingestion.models import OutboxEvent
from app.modules.memory import evidence as memory_evidence

TARGET_SOURCE = "source"
SOURCE_REVOKED_CODE = "source_revoked"
SECRETS_UNAVAILABLE = "connector_secrets_unavailable"
INVALID_SOURCE = "invalid_source"


def require_admin(
    tenant: TenantRequest, session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> svc.WorkspaceCtx:
    ctx = svc.resolve_workspace(tenant, session, org_id, workspace_id, Action.WORKSPACE_UPDATE)
    ctx.require(
        Action.WORKSPACE_UPDATE, target_type=svc.TARGET_WORKSPACE, target_id=str(workspace_id)
    )
    return ctx


def audit(
    tenant: TenantRequest,
    session: Session,
    event: AuditEventType,
    ctx: svc.WorkspaceCtx,
    source_id: uuid.UUID,
    metadata: dict[str, object] | None = None,
) -> None:
    record_event(
        session,
        event_type=event,
        actor_user_id=tenant.user_id,
        org_id=ctx.org_id,
        target_type=TARGET_SOURCE,
        target_id=str(source_id),
        outcome="success",
        request_id=tenant.request_id,
        metadata={"workspace_id": str(ctx.workspace_id), **(metadata or {})},
    )


def request_sync(
    session: Session,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    trigger: str,
    delivery_id: str | None = None,
) -> uuid.UUID | None:
    """Queue a sync + its ``source.sync.requested`` outbox row, atomically and idempotently.

    ``None`` when nothing was added: the source is not active, a sync is already queued, or this
    webhook ``delivery_id`` was seen before (unique indexes + ``ON CONFLICT DO NOTHING``).
    """
    active = session.execute(
        select(Source.id).where(Source.id == source_id, Source.state == SOURCE_STATE_ACTIVE)
    ).scalar_one_or_none()
    if active is None:
        return None
    sync_id = uuid.uuid4()
    inserted = session.execute(
        pg_insert(SourceSync)
        .values(
            id=sync_id,
            org_id=org_id,
            source_id=source_id,
            state=SYNC_QUEUED,
            trigger=trigger,
            delivery_id=delivery_id,
        )
        .on_conflict_do_nothing()
        .returning(SourceSync.id)
    ).scalar_one_or_none()
    if inserted is None:
        return None
    session.execute(
        insert(OutboxEvent).values(
            id=uuid.uuid4(),
            org_id=org_id,
            aggregate_type=AGGREGATE_SOURCE,
            aggregate_id=source_id,
            event_type=EVENT_SOURCE_SYNC_REQUESTED,
            payload={"source_id": str(source_id), "sync_id": str(sync_id)},
        )
    )
    return sync_id


def create_source(
    tenant: TenantRequest,
    session: Session,
    settings: Settings,
    ctx: svc.WorkspaceCtx,
    body: CreateSource,
) -> SourceCreated:
    try:
        config = GitHubConnector.validate_config(
            {"repo": body.repo, "ref": body.ref, "include": body.include}
        )
    except ValueError:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, INVALID_SOURCE) from None
    account_id, source_id = uuid.uuid4(), uuid.uuid4()
    token_ct: bytes | None = None
    key_id: str | None = None
    webhook_secret = secrets.token_urlsafe(32)
    hook_ct: bytes | None = None
    hook_key: str | None = None
    try:
        cipher = crypto.cipher_from_settings(settings)
    except crypto.CryptoError:
        if body.token is not None:
            raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, SECRETS_UNAVAILABLE) from None
        cipher = None  # public repository, no webhook: no secret needs storing
    if cipher is not None:
        if body.token is not None:
            sealed = cipher.seal(body.token.get_secret_value(), aad=account_id.bytes)
            token_ct, key_id = sealed.ciphertext, sealed.key_id
        sealed_hook = cipher.seal(webhook_secret, aad=source_id.bytes)
        hook_ct, hook_key = sealed_hook.ciphertext, sealed_hook.key_id
    interval = body.sync_interval_seconds or settings.CONNECTOR_SYNC_DEFAULT_INTERVAL_SECONDS
    session.execute(
        insert(ConnectorAccount).values(
            id=account_id,
            org_id=ctx.org_id,
            provider=PROVIDER_GITHUB,
            label=config["repo"],
            token_ciphertext=token_ct,
            key_id=key_id,
            created_by=tenant.user_id,
        )
    )
    session.execute(
        insert(Source).values(
            id=source_id,
            org_id=ctx.org_id,
            workspace_id=ctx.workspace_id,
            kind="connector",
            name=config["repo"],
            config=config,
            created_by=tenant.user_id,
            connector_account_id=account_id,
            sync_interval_seconds=interval,
            next_sync_at=func.now() + timedelta(seconds=interval),
            webhook_secret_ciphertext=hook_ct,
            webhook_key_id=hook_key,
        )
    )
    audit(
        tenant,
        session,
        AuditEventType.SOURCE_CREATED,
        ctx,
        source_id,
        {"provider": PROVIDER_GITHUB, "has_token": token_ct is not None},
    )
    request_sync(session, ctx.org_id, source_id, trigger="manual")
    out = read_source(session, ctx, source_id)
    assert out is not None  # noqa: S101 - just inserted
    return SourceCreated(
        **out.model_dump(),
        webhook_secret=webhook_secret if hook_ct is not None else None,
        webhook_path=f"/v1/webhooks/github/{source_id}",
    )


def _source_out(row: Any, has_token: bool) -> SourceOut:
    return SourceOut(
        id=row.id,
        workspace_id=row.workspace_id,
        provider=PROVIDER_GITHUB,
        name=row.name,
        config=dict(row.config),
        state=row.state,
        has_token=has_token,
        sync_interval_seconds=row.sync_interval_seconds or 0,
        next_sync_at=row.next_sync_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def read_source(
    session: Session, ctx: svc.WorkspaceCtx, source_id: uuid.UUID, *, detail: bool = True
) -> SourceOut | None:
    row = session.execute(
        select(
            Source.id,
            Source.workspace_id,
            Source.name,
            Source.config,
            Source.state,
            Source.sync_interval_seconds,
            Source.next_sync_at,
            Source.created_at,
            Source.revoked_at,
            ConnectorAccount.token_ciphertext.is_not(None).label("has_token"),
        )
        .join(ConnectorAccount, ConnectorAccount.id == Source.connector_account_id)
        .where(
            Source.id == source_id,
            Source.workspace_id == ctx.workspace_id,
            Source.org_id == ctx.org_id,
            Source.kind == "connector",
        )
    ).one_or_none()
    if row is None:
        return None
    out = _source_out(row, bool(row.has_token))
    if detail:
        last = session.execute(
            select(SourceSync)
            .where(SourceSync.source_id == source_id)
            .order_by(SourceSync.created_at.desc(), SourceSync.id)
            .limit(1)
        ).scalar_one_or_none()
        if last is not None:
            out.last_sync = SyncSummary(
                id=last.id,
                state=last.state,
                trigger=last.trigger,
                error_code=last.error_code,
                stats=dict(last.stats),
                created_at=last.created_at,
                started_at=last.started_at,
                finished_at=last.finished_at,
            )
        counts = dict(
            session.execute(
                select(SourceItem.state, func.count())
                .where(SourceItem.source_id == source_id)
                .group_by(SourceItem.state)
            ).all()
        )
        out.items = ItemCounts(
            active=counts.get(ITEM_ACTIVE, 0), deleted=counts.get(ITEM_DELETED, 0)
        )
    return out


def list_sources(session: Session, ctx: svc.WorkspaceCtx) -> list[SourceOut]:
    ids = session.execute(
        select(Source.id)
        .where(
            Source.workspace_id == ctx.workspace_id,
            Source.org_id == ctx.org_id,
            Source.kind == "connector",
        )
        .order_by(Source.created_at, Source.id)
    ).scalars()
    return [o for i in ids if (o := read_source(session, ctx, i, detail=False)) is not None]


def revoke_source(
    tenant: TenantRequest, session: Session, ctx: svc.WorkspaceCtx, source_id: uuid.UUID
) -> SourceOut:
    """Revoke (idempotent). See the module docstring for what happens in this one transaction."""
    row = session.execute(
        select(Source.id, Source.connector_account_id)
        .where(
            Source.id == source_id,
            Source.workspace_id == ctx.workspace_id,
            Source.org_id == ctx.org_id,
            Source.kind == "connector",
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise not_found()
    session.execute(
        update(Source)
        .where(Source.id == source_id)
        .values(
            state=SOURCE_STATE_REVOKED,
            revoked_at=func.coalesce(Source.revoked_at, func.now()),
            next_sync_at=None,
            webhook_secret_ciphertext=None,
            webhook_key_id=None,
        )
    )
    session.execute(
        update(ConnectorAccount)
        .where(ConnectorAccount.id == row.connector_account_id)
        .values(
            token_ciphertext=None,
            key_id=None,
            revoked_at=func.coalesce(ConnectorAccount.revoked_at, func.now()),
        )
    )
    session.execute(
        update(SourceSync)
        .where(SourceSync.source_id == source_id, SourceSync.state == SYNC_QUEUED)
        .values(state=SYNC_CANCELLED, error_code=SOURCE_REVOKED_CODE, finished_at=func.now())
    )
    deleted = (
        session.execute(
            update(Document)
            .where(
                Document.source_id == source_id,
                Document.org_id == ctx.org_id,
                Document.deleted_at.is_(None),
            )
            .values(deleted_at=func.now(), deleted_by=tenant.user_id)
            .returning(Document.id)
        )
        .scalars()
        .all()
    )
    for document_id in deleted:
        enqueue_document_deleted(
            session, org_id=ctx.org_id, workspace_id=ctx.workspace_id, document_id=document_id
        )
    session.execute(
        update(SourceItem)
        .where(SourceItem.source_id == source_id, SourceItem.state == ITEM_ACTIVE)
        .values(state=ITEM_DELETED)
    )
    # M07: memories whose evidence came from this source are no longer valid.
    memory_evidence.invalidate_documents(session, ctx.org_id, deleted, "source_revoked")
    audit(
        tenant,
        session,
        AuditEventType.SOURCE_REVOKED,
        ctx,
        source_id,
        {"documents_deleted": len(deleted)},
    )
    out = read_source(session, ctx, source_id)
    assert out is not None  # noqa: S101 - locked above
    return out
