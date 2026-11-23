"""GitHub webhook ingestion (M06.01.04; contract m06 section 2).

``POST /v1/webhooks/github/{source_id}`` is unauthenticated by design: the per-source secret signs
the body (``X-Hub-Signature-256: sha256=<hex HMAC>``), verified in constant time. Every failure
(unknown source, revoked, no secret, bad signature) is the same bodyless-detail 401, and the HMAC is
computed even when there is no secret, so neither status nor timing tells a source exists. A valid
``push`` queues a sync (``source.sync.requested`` via the outbox); a repeated ``X-GitHub-Delivery``
id inserts nothing (unique index) and is still answered 202. Other events (``ping``...) are ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.api.deps import api_error
from app.core.config import Settings, get_settings
from app.core.db import TenantSessionFactory, get_tenant_session_factory
from app.modules.connectors import crypto
from app.modules.connectors.models import SOURCE_STATE_ACTIVE
from app.modules.connectors.service import request_sync

router = APIRouter(prefix="/v1/webhooks", tags=["connectors"])

MAX_BODY_BYTES = 1_048_576
_DUMMY_SECRET = b"\x00" * 32


@router.post("/github/{source_id}", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    source_id: uuid.UUID,
    sessions: Annotated[TenantSessionFactory, Depends(get_tenant_session_factory)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise api_error(413, "payload_too_large")
    return await run_in_threadpool(
        _verify_and_queue,
        sessions,
        settings,
        source_id,
        body,
        x_hub_signature_256,
        x_github_event,
        x_github_delivery,
    )


def _verify_and_queue(
    sessions: TenantSessionFactory,
    settings: Settings,
    source_id: uuid.UUID,
    body: bytes,
    signature: str | None,
    event: str | None,
    delivery_header: str | None,
) -> dict[str, str]:
    with sessions(user_id=None, org_id=None) as session:
        row = session.execute(
            text("SELECT org_id, state, secret, key_id FROM connector_webhook_lookup(:s)"),
            {"s": source_id},
        ).one_or_none()
    org_id: uuid.UUID | None = None
    secret = _DUMMY_SECRET
    if row is not None and row.secret is not None and row.state == SOURCE_STATE_ACTIVE:
        try:
            # The org's data keys are read under that org's context (tenant_keys is FORCE RLS).
            with sessions(user_id=None, org_id=row.org_id) as key_session:
                secret = (
                    crypto.secret_cipher(key_session, row.org_id, settings)
                    .open(bytes(row.secret), row.key_id or "", aad=source_id.bytes)
                    .encode("utf-8")
                )
            org_id = row.org_id
        except crypto.CryptoError:
            secret = _DUMMY_SECRET
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if org_id is None or not hmac.compare_digest(expected.encode(), (signature or "").encode()):
        raise api_error(status.HTTP_401_UNAUTHORIZED, "unauthorized")
    if event != "push":
        return {"status": "ignored"}
    delivery = (delivery_header or "")[:128] or None
    with sessions(user_id=None, org_id=org_id) as session:
        sync_id = request_sync(session, org_id, source_id, trigger="webhook", delivery_id=delivery)
    return {"status": "queued" if sync_id else "duplicate"}
