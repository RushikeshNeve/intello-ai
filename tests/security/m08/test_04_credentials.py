"""M08.01.04: connector key rotation, webhook secret handling, revoke, and a canary-secret sweep.

Real PostgreSQL, the real API (in-process). The rotation command is
``app.modules.connectors.rotate``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from catalog_support import World
from fake_github import FakeGitHub
from m06_support import create_source, rows, run, scalar
from pydantic import SecretStr
from sqlalchemy import Engine

from app.core.config import Settings
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.modules.connectors import crypto
from app.modules.connectors.rotate import rotate_secrets
from app.modules.connectors.sync import SyncDeps
from app.modules.connectors.wiring import default_connector_factory

CANARY_TOKEN = "ghp_CANARY_m08_token_0123456789abcdef"
KEY1 = base64.b64encode(bytes(range(32))).decode()
KEY2 = base64.b64encode(bytes(range(100, 132))).decode()


def _settings(base: Settings, **over: Any) -> Settings:
    return base.model_copy(update=over)


@pytest.fixture
def settings_k1(sholder: Any) -> Iterator[Settings]:
    original = sholder.current
    yield original
    sholder.current = original


def _k2(base: Settings, *, with_old: bool = True) -> Settings:
    return _settings(
        base,
        CONNECTOR_ENCRYPTION_KEY=SecretStr(KEY2),
        CONNECTOR_ENCRYPTION_KEY_ID="k2",
        CONNECTOR_ENCRYPTION_OLD_KEYS=SecretStr(f"k1:{KEY1}") if with_old else None,
    )


def _sig(secret: str, body: bytes) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "X-GitHub-Event": "push"}


def _row(engine: Engine, source_id: str) -> Any:
    return rows(
        engine,
        "SELECT a.id AS account_id, a.token_ciphertext, a.key_id, a.revoked_at,"
        " s.webhook_secret_ciphertext, s.webhook_key_id, s.state"
        " FROM sources s JOIN connector_accounts a ON a.id = s.connector_account_id"
        " WHERE s.id = :s",
        s=uuid.UUID(source_id),
    )[0]


def test_key_rotation_old_tokens_decrypt_then_reseal_under_the_new_key(
    world: World, admin_engine: Engine, sholder: Any, settings_k1: Settings
) -> None:
    created = create_source(world, token=CANARY_TOKEN)
    sid, secret = created["id"], created["webhook_secret"]
    before = _row(admin_engine, sid)
    assert (before.key_id, before.webhook_key_id) == ("k1", "k1")

    new_cipher = crypto.cipher_from_settings(_k2(settings_k1))
    # old ciphertext still opens through CONNECTOR_ENCRYPTION_OLD_KEYS
    assert (
        new_cipher.open(bytes(before.token_ciphertext), "k1", aad=before.account_id.bytes)
        == CANARY_TOKEN
    )
    # without the old key list it cannot: rotation must precede removing the old key
    only_new = crypto.cipher_from_settings(_k2(settings_k1, with_old=False))
    with pytest.raises(crypto.DecryptionFailed):
        only_new.open(bytes(before.token_ciphertext), "k1", aad=before.account_id.bytes)

    # a pass that cannot open the old rows fails them and leaves them untouched
    with admin_engine.begin() as conn:
        report = rotate_secrets(conn, only_new)
    assert report.failed >= 2
    assert _row(admin_engine, sid).key_id == "k1"

    # dry run changes nothing; the real run re-seals token and webhook secret under k2
    with admin_engine.begin() as conn:
        dry = rotate_secrets(conn, new_cipher, dry_run=True)
    assert dry.tokens >= 1 and dry.webhook_secrets >= 1
    assert _row(admin_engine, sid).key_id == "k1"
    with admin_engine.begin() as conn:
        done = rotate_secrets(conn, new_cipher)
    assert done.failed == 0 and done.tokens >= 1 and done.webhook_secrets >= 1
    after = _row(admin_engine, sid)
    assert (after.key_id, after.webhook_key_id) == ("k2", "k2")
    assert bytes(after.token_ciphertext) != bytes(before.token_ciphertext)
    # the NEW key alone now opens everything (the old key can be dropped)
    assert only_new.open(bytes(after.token_ciphertext), "k2", aad=after.account_id.bytes) == (
        CANARY_TOKEN
    )
    with admin_engine.begin() as conn:
        again = rotate_secrets(conn, new_cipher)
    assert (again.tokens, again.webhook_secrets, again.failed) == (0, 0, 0)  # idempotent

    # the webhook secret value is unchanged: the API (now on key k2 only) still verifies it
    sholder.current = _k2(settings_k1, with_old=False)
    body = b'{"ref":"refs/heads/main"}'
    client = world.api.client
    ok = client.post(f"/v1/webhooks/github/{sid}", content=body, headers=_sig(secret, body))
    assert ok.status_code == 202
    bad = client.post(f"/v1/webhooks/github/{sid}", content=body, headers=_sig("nope", body))
    assert bad.status_code == 401


def test_revoke_wipes_ciphertext_and_blocks_access(
    world: World, admin_engine: Engine, settings_k1: Settings
) -> None:
    created = create_source(world, token=CANARY_TOKEN)
    sid, secret = created["id"], created["webhook_secret"]
    api = world.api
    assert api.post(world.path(f"/sources/{sid}/revoke"), world.ws_admin, {}).status == 200
    row = _row(admin_engine, sid)
    assert row.token_ciphertext is None and row.key_id is None and row.revoked_at is not None
    assert row.webhook_secret_ciphertext is None and row.webhook_key_id is None
    assert row.state == "revoked"
    assert api.get(world.path(f"/sources/{sid}"), world.ws_admin).body["has_token"] is False
    assert api.post(world.path(f"/sources/{sid}/sync"), world.ws_admin, {}).status != 202
    body = b"{}"
    hook = api.client.post(f"/v1/webhooks/github/{sid}", content=body, headers=_sig(secret, body))
    assert hook.status_code == 401  # the secret died with the source
    # revoking again is idempotent and rotation has nothing left to re-seal for it
    assert api.post(world.path(f"/sources/{sid}/revoke"), world.ws_admin, {}).status == 200


def test_canary_secrets_never_reach_logs_audit_outbox_or_api_responses(
    world: World,
    admin_engine: Engine,
    worker_engine: Engine,
    settings_k1: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    api = world.api
    fake = FakeGitHub()
    try:
        fake.commit({"README.md": b"hello canary"})
        settings = settings_k1.model_copy(update={"GITHUB_API_BASE_URL": fake.url})
        deps = SyncDeps(
            engine=worker_engine,
            settings=settings,
            store=InMemoryObjectStore(),
            connector_factory=default_connector_factory(settings),
        )
        created = create_source(world, token=CANARY_TOKEN)
        sid, secret = created["id"], created["webhook_secret"]
        assert CANARY_TOKEN not in str(created)  # the token is never echoed, the secret once
        responses: list[Any] = [
            api.get(world.path(f"/sources/{sid}"), world.ws_admin).body,
            api.get(world.path("/sources"), world.ws_admin).body,
            api.get(world.path(f"/sources/{sid}"), world.editor).body,  # denied
        ]
        assert run(deps, world, sid) == "succeeded"
        body = b"{}"
        wh = api.client.post(f"/v1/webhooks/github/{sid}", content=body, headers=_sig(secret, body))
        responses.append(wh.text)
        responses.append(api.post(world.path(f"/sources/{sid}/revoke"), world.ws_admin, {}).body)
    finally:
        fake.close()

    surfaces: dict[str, str] = {
        "responses": str(responses),
        "logs": "\n".join(r.getMessage() + str(r.__dict__.get("args", "")) for r in caplog.records),
    }
    for table in (
        "audit_events",
        "outbox_events",
        "sources",
        "source_syncs",
        "source_items",
        "connector_accounts",
        "documents",
    ):
        found = rows(admin_engine, f"SELECT t::text AS j FROM {table} t")
        surfaces[table] = "\n".join(r.j for r in found)
    for name, blob in surfaces.items():
        assert CANARY_TOKEN not in blob, f"token canary found in {name}"
    for name, blob in surfaces.items():
        if name != "creation":
            assert secret not in blob, f"webhook secret found in {name}"
    assert scalar(admin_engine, "SELECT 1") == 1
