"""The sync run (M06.01.04/.05; contract m06 section 2), executed by the ``connectors.sync_source``
Celery task as the worker role under ``app.current_org_id``.

Shape: claim the queued sync row (``running``; the partial unique index allows one per source) ->
decrypt the token -> ``discover`` -> per item: fetch (no transaction open), then ONE transaction
that
re-checks the source is still ``active`` (``FOR SHARE`` on the source row, so a concurrent revoke
serializes with it), writes the document/version through ``documents.remote_ingest`` and upserts the
item map -> after the whole page, in one transaction, deletions (full listings reconcile what the
page did not mention), then the cursor advances. A failure keeps the old cursor: the next run
re-discovers from it and the item map (``(source_id, remote_id)``, ``version_token``, content
sha256) turns the replay into no-ops, so nothing is duplicated.

Secrets: the token is decrypted into a local, handed to the connector factory and never logged;
errors surface only as stable codes.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.models import Document, Source
from app.modules.audit import AuditEventType, record_event
from app.modules.connectors import crypto
from app.modules.connectors.models import (
    ITEM_ACTIVE,
    ITEM_DELETED,
    SOURCE_STATE_ACTIVE,
    SOURCE_STATE_ERROR,
    SYNC_CANCELLED,
    SYNC_FAILED,
    SYNC_QUEUED,
    SYNC_RUNNING,
    SYNC_SUCCEEDED,
    ConnectorAccount,
    SourceItem,
    SourceSync,
)
from app.modules.connectors.port import (
    ConnectorError,
    ItemSkipped,
    RemoteItem,
    SourceConnector,
)
from app.modules.documents.remote_ingest import DocumentGone, QuotaExceeded, ingest_bytes
from app.modules.ingestion.jobs import enqueue_document_deleted
from app.modules.storage.ports import ObjectStore
from app.workers.worker_db import worker_session

log = logging.getLogger("ukos.connectors.sync")

MAX_PAGES = 50
MAX_BACKOFF_SECONDS = 3600
DOCUMENT_TARGET = "document"

ConnectorFactory = Callable[[dict[str, Any], str | None], SourceConnector]


class SyncBusy(Exception):
    """Another sync of this source is running; the task retries later."""


class SourceRevoked(Exception):
    """The source stopped being active while syncing: stop writing."""


@dataclass
class SyncDeps:
    engine: Engine
    settings: Settings
    store: ObjectStore
    connector_factory: ConnectorFactory


@dataclass(frozen=True)
class _Claimed:
    sync_id: uuid.UUID
    workspace_id: uuid.UUID
    created_by: uuid.UUID
    config: dict[str, Any]
    cursor: str | None
    interval: int
    account_id: uuid.UUID
    token_ciphertext: bytes | None
    key_id: str | None


def _claim(deps: SyncDeps, org_id: uuid.UUID, source_id: uuid.UUID) -> _Claimed | None:
    """queued -> running. ``None`` = nothing to do (no queued sync, or the source is not active)."""
    with worker_session(deps.engine, org_id) as session:
        src = session.execute(
            select(
                Source.workspace_id,
                Source.created_by,
                Source.config,
                Source.sync_cursor,
                Source.sync_interval_seconds,
                Source.state,
                Source.connector_account_id,
            ).where(Source.id == source_id, Source.kind == "connector")
        ).one_or_none()
        queued = session.execute(
            select(SourceSync.id)
            .where(SourceSync.source_id == source_id, SourceSync.state == SYNC_QUEUED)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if queued is None:
            return None
        if src is None or src.state != SOURCE_STATE_ACTIVE or src.connector_account_id is None:
            session.execute(
                update(SourceSync)
                .where(SourceSync.id == queued)
                .values(state=SYNC_CANCELLED, error_code="source_inactive", finished_at=func.now())
            )
            return None
        try:
            with session.begin_nested():
                session.execute(
                    update(SourceSync)
                    .where(SourceSync.id == queued)
                    .values(
                        state=SYNC_RUNNING, started_at=func.now(), cursor_before=src.sync_cursor
                    )
                )
        except IntegrityError:
            raise SyncBusy from None
        account = session.execute(
            select(ConnectorAccount.token_ciphertext, ConnectorAccount.key_id).where(
                ConnectorAccount.id == src.connector_account_id,
                ConnectorAccount.revoked_at.is_(None),
            )
        ).one_or_none()
        if account is None:
            session.execute(
                update(SourceSync)
                .where(SourceSync.id == queued)
                .values(state=SYNC_CANCELLED, error_code="account_revoked", finished_at=func.now())
            )
            return None
        return _Claimed(
            sync_id=queued,
            workspace_id=src.workspace_id,
            created_by=src.created_by,
            config=dict(src.config),
            cursor=src.sync_cursor,
            interval=src.sync_interval_seconds or 3600,
            account_id=src.connector_account_id,
            token_ciphertext=bytes(account.token_ciphertext) if account.token_ciphertext else None,
            key_id=account.key_id,
        )


def _assert_active(session: Session, source_id: uuid.UUID) -> None:
    """Share-lock the source row; a revoke (which updates it) waits for this transaction."""
    state = session.execute(
        select(Source.state).where(Source.id == source_id).with_for_update(read=True)
    ).scalar_one_or_none()
    if state != SOURCE_STATE_ACTIVE:
        raise SourceRevoked


def _item_row(session: Session, source_id: uuid.UUID, remote_id: str) -> Any:
    return session.execute(
        select(
            SourceItem.id,
            SourceItem.document_id,
            SourceItem.version_token,
            SourceItem.content_sha256,
            SourceItem.state,
        ).where(SourceItem.source_id == source_id, SourceItem.remote_id == remote_id)
    ).one_or_none()


def _upsert_item(
    session: Session,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    item: RemoteItem,
    document_id: uuid.UUID | None,
    sha: str | None,
) -> None:
    values = {
        "path": item.path,
        "document_id": document_id,
        "version_token": item.version_token,
        "content_sha256": sha,
        "state": ITEM_ACTIVE,
        "last_seen_at": func.now(),
    }
    session.execute(
        pg_insert(SourceItem)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            source_id=source_id,
            remote_id=item.remote_id,
            **values,
        )
        .on_conflict_do_update(constraint="uq_source_items_source_id_remote_id", set_=values)
    )


def _delete_documents(
    session: Session, org_id: uuid.UUID, claim: _Claimed, document_ids: list[uuid.UUID]
) -> None:
    for document_id in document_ids:
        done = session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.org_id == org_id,
                Document.deleted_at.is_(None),
            )
            .values(deleted_at=func.now(), deleted_by=claim.created_by)
            .returning(Document.id)
        ).scalar_one_or_none()
        if done is not None:
            enqueue_document_deleted(
                session, org_id=org_id, workspace_id=claim.workspace_id, document_id=document_id
            )
            record_event(
                session,
                event_type=AuditEventType.DOCUMENT_DELETED,
                actor_user_id=None,
                org_id=org_id,
                target_type=DOCUMENT_TARGET,
                target_id=str(document_id),
                outcome="success",
                request_id=None,
                metadata={"workspace_id": str(claim.workspace_id), "reason": "source_item_removed"},
            )


def _remove_items(
    session: Session,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    claim: _Claimed,
    remote_ids: list[str],
) -> int:
    rows = session.execute(
        select(SourceItem.id, SourceItem.document_id).where(
            SourceItem.source_id == source_id,
            SourceItem.remote_id.in_(remote_ids),
            SourceItem.state == ITEM_ACTIVE,
        )
    ).all()
    _delete_documents(session, org_id, claim, [r.document_id for r in rows if r.document_id])
    if rows:
        session.execute(
            update(SourceItem)
            .where(SourceItem.id.in_([r.id for r in rows]))
            .values(state=ITEM_DELETED, last_seen_at=func.now())
        )
    return len(rows)


def _process_item(
    deps: SyncDeps,
    connector: SourceConnector,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    claim: _Claimed,
    item: RemoteItem,
    stats: dict[str, int],
) -> None:
    if item.deleted:
        with worker_session(deps.engine, org_id) as session:
            _assert_active(session, source_id)
            stats["deleted"] += _remove_items(session, org_id, source_id, claim, [item.remote_id])
        return
    with worker_session(deps.engine, org_id) as session:
        existing = _item_row(session, source_id, item.remote_id)
    if (
        existing is not None
        and existing.state == ITEM_ACTIVE
        and item.version_token
        and existing.version_token == item.version_token
    ):
        stats["unchanged"] += 1
        return
    try:
        fetched = connector.fetch(item)
    except ItemSkipped:
        stats["skipped"] += 1
        return
    if b"\x00" in fetched.data[:8192] and fetched.content_type.startswith("text/"):
        stats["skipped"] += 1  # a "text" file that is binary
        return
    if not fetched.data:
        stats["skipped"] += 1
        return
    filename = item.path.rsplit("/", 1)[-1][:255] or "file"
    sha = hashlib.sha256(fetched.data).hexdigest()
    with worker_session(deps.engine, org_id) as session:
        _assert_active(session, source_id)
        existing = _item_row(session, source_id, item.remote_id)
        if (
            existing is not None
            and existing.state == ITEM_ACTIVE
            and existing.content_sha256 == sha
        ):
            # Same bytes under a new upstream token (e.g. a touched file): no new version.
            _upsert_item(session, org_id, source_id, item, existing.document_id, sha)
            stats["unchanged"] += 1
            return
        document_id = existing.document_id if existing and existing.state == ITEM_ACTIVE else None
        try:
            result = ingest_bytes(
                session,
                deps.store,
                org_id=org_id,
                workspace_id=claim.workspace_id,
                source_id=source_id,
                created_by=claim.created_by,
                data=fetched.data,
                filename=filename,
                content_type=fetched.content_type,
                title=item.path[:255].strip() or filename,
                document_id=document_id,
                audit_metadata={"connector": "sync", "sync_id": str(claim.sync_id)},
            )
        except DocumentGone:
            result = ingest_bytes(
                session,
                deps.store,
                org_id=org_id,
                workspace_id=claim.workspace_id,
                source_id=source_id,
                created_by=claim.created_by,
                data=fetched.data,
                filename=filename,
                content_type=fetched.content_type,
                title=item.path[:255].strip() or filename,
                audit_metadata={"connector": "sync", "sync_id": str(claim.sync_id)},
            )
        except QuotaExceeded:
            stats["skipped"] += 1
            return
        _upsert_item(session, org_id, source_id, item, result.document_id, result.sha256)
        stats["new" if result.created else "updated"] += 1


def _finish(
    deps: SyncDeps,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    sync_id: uuid.UUID,
    *,
    state: str,
    stats: dict[str, int],
    error_code: str | None = None,
    cursor_after: str | None = None,
    next_delay: int | None = None,
    source_state: str | None = None,
) -> None:
    with worker_session(deps.engine, org_id) as session:
        values: dict[str, Any] = {"state": state, "stats": stats, "finished_at": func.now()}
        if error_code:
            values["error_code"] = error_code
        if cursor_after is not None:
            values["cursor_after"] = cursor_after
        session.execute(
            update(SourceSync)
            .where(SourceSync.id == sync_id, SourceSync.state == SYNC_RUNNING)
            .values(**values)
        )
        if next_delay is not None or source_state is not None:
            src_values: dict[str, Any] = {}
            if next_delay is not None:
                src_values["next_sync_at"] = func.now() + timedelta(seconds=next_delay)
            if source_state is not None:
                src_values["state"] = source_state
                src_values["next_sync_at"] = None
            # Never resurrect a revoked source: only an ``active`` one is touched.
            session.execute(
                update(Source)
                .where(Source.id == source_id, Source.state == SOURCE_STATE_ACTIVE)
                .values(**src_values)
            )


def _advance_cursor(
    session: Session, source_id: uuid.UUID, cursor: str | None, interval: int
) -> None:
    done = session.execute(
        update(Source)
        .where(Source.id == source_id, Source.state == SOURCE_STATE_ACTIVE)
        .values(
            sync_cursor=cursor,
            next_sync_at=func.now() + timedelta(seconds=interval),
        )
        .returning(Source.id)
    ).scalar_one_or_none()
    if done is None:
        raise SourceRevoked


def run_source_sync(deps: SyncDeps, org_id: uuid.UUID, source_id: uuid.UUID) -> str:
    """Run the oldest queued sync of ``source_id``. Returns an outcome label (never a secret)."""
    claim = _claim(deps, org_id, source_id)
    if claim is None:
        return "skipped"
    stats = {"new": 0, "updated": 0, "unchanged": 0, "deleted": 0, "skipped": 0}
    try:
        token: str | None = None
        if claim.token_ciphertext is not None:
            cipher = crypto.cipher_from_settings(deps.settings)
            token = cipher.open(
                claim.token_ciphertext, claim.key_id or "", aad=claim.account_id.bytes
            )
        connector = deps.connector_factory(claim.config, token)
        cursor = claim.cursor
        for _page in range(MAX_PAGES):
            page = connector.discover(cursor)
            seen: set[str] = set()
            for item in page.items:
                seen.add(item.remote_id)
                _process_item(deps, connector, org_id, source_id, claim, item, stats)
            with worker_session(deps.engine, org_id) as session:
                _assert_active(session, source_id)
                if page.full_listing:
                    stale = session.execute(
                        select(SourceItem.remote_id).where(
                            SourceItem.source_id == source_id, SourceItem.state == ITEM_ACTIVE
                        )
                    ).scalars()
                    gone = [r for r in stale if r not in seen]
                    if gone:
                        stats["deleted"] += _remove_items(session, org_id, source_id, claim, gone)
                _advance_cursor(session, source_id, page.next_cursor, claim.interval)
            cursor = page.next_cursor
            if not page.has_more:
                break
        _finish(
            deps,
            org_id,
            source_id,
            claim.sync_id,
            state=SYNC_SUCCEEDED,
            stats=stats,
            cursor_after=cursor,
        )
        return "succeeded"
    except SourceRevoked:
        _finish(
            deps,
            org_id,
            source_id,
            claim.sync_id,
            state=SYNC_CANCELLED,
            stats=stats,
            error_code="source_revoked",
        )
        return "revoked"
    except ConnectorError as exc:
        fatal = exc.code in ("auth_failed", "repo_not_found")
        delay = exc.retry_after_seconds or min(MAX_BACKOFF_SECONDS, max(claim.interval, 300))
        _finish(
            deps,
            org_id,
            source_id,
            claim.sync_id,
            state=SYNC_FAILED,
            stats=stats,
            error_code=exc.code,
            next_delay=delay,
            source_state=SOURCE_STATE_ERROR if fatal else None,
        )
        log.warning("sync %s failed: %s", claim.sync_id, exc.code)
        return "failed"
    except crypto.CryptoError:
        _finish(
            deps,
            org_id,
            source_id,
            claim.sync_id,
            state=SYNC_FAILED,
            stats=stats,
            error_code="secret_unavailable",
            source_state=SOURCE_STATE_ERROR,
        )
        log.error("sync %s: connector secret cannot be decrypted", claim.sync_id)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - recorded, never re-raised (no retry storm)
        _finish(
            deps,
            org_id,
            source_id,
            claim.sync_id,
            state=SYNC_FAILED,
            stats=stats,
            error_code="internal_error",
            next_delay=min(MAX_BACKOFF_SECONDS, max(claim.interval, 300)),
        )
        log.error("sync %s failed unexpectedly (%s)", claim.sync_id, type(exc).__name__)
        return "failed"
