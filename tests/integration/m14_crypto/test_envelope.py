"""M14.08: per-org data keys wrapped by a master key; dual read of legacy rows, rotation, shred,
and a canary sweep proving no key material reaches logs, audit rows or API responses."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from catalog_support import World
from fake_github import FakeGitHub
from fastapi.testclient import TestClient
from m06_support import rows, run, scalar
from m14_support import CANARY, KEK1, KEK2
from pydantic import SecretStr
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.core.crypto import rotate as ops
from app.core.crypto.envelope import (
    TenantCipher,
    issue_new_dek,
    load_active_dek,
    shred_org_keys,
    wrap_context,
)
from app.core.crypto.errors import DecryptionFailed
from app.core.crypto.providers import provider_from_settings
from app.modules.connectors import crypto
from app.modules.connectors.sync import SyncDeps
from app.workers.worker_db import worker_session


def _create(world: World, org: uuid.UUID, ws: uuid.UUID, owner: Any) -> dict[str, Any]:
    body = {"provider": "github", "repo": "acme/docs", "token": CANARY}
    res = world.api.post(f"/v1/orgs/{org}/workspaces/{ws}/sources", owner, body)
    assert res.status == 201, res.body
    return dict(res.body)


def _account(engine: Engine, source_id: str) -> Any:
    return rows(
        engine,
        "SELECT a.id, a.org_id, a.token_ciphertext, a.key_id, s.webhook_key_id"
        " FROM sources s JOIN connector_accounts a ON a.id = s.connector_account_id"
        " WHERE s.id = :s",
        s=uuid.UUID(source_id),
    )[0]


def _to_legacy(admin: Engine, settings: Settings, source_id: str) -> None:
    """Rewrite a source's secrets into the pre-M14 single-key format (key id k1)."""
    legacy = crypto.cipher_from_settings(settings)
    acc = _account(admin, source_id)
    with admin.begin() as conn:
        t = legacy.seal(CANARY, aad=acc.id.bytes)
        conn.execute(
            text("UPDATE connector_accounts SET token_ciphertext = :c, key_id = :k WHERE id = :i"),
            {"c": t.ciphertext, "k": t.key_id, "i": acc.id},
        )


def _sig(secret: str, body: bytes) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "X-GitHub-Event": "push"}


def test_new_secrets_use_the_org_data_key_and_stay_usable(
    world: World, admin_engine: Engine, deps: SyncDeps, fake_gh: FakeGitHub, client: TestClient
) -> None:
    created = _create(world, world.org_id, world.ws, world.ws_admin)
    acc = _account(admin_engine, created["id"])
    assert acc.key_id.startswith("dek:")
    assert acc.webhook_key_id == acc.key_id
    key = rows(admin_engine, "SELECT * FROM tenant_keys WHERE org_id = :o", o=world.org_id)
    assert len(key) == 1
    assert key[0].state == "active"
    assert key[0].kek_id == "kek1"
    assert acc.key_id == f"dek:{key[0].id}"
    assert len(key[0].wrapped_key) == 60
    assert CANARY.encode() not in bytes(acc.token_ciphertext)
    assert run(deps, world, created["id"]) == "succeeded"
    assert set(fake_gh.auth_seen) == {f"Bearer {CANARY}"}
    body = b'{"ref":"refs/heads/main"}'
    hook = client.post(
        f"/v1/webhooks/github/{created['id']}",
        content=body,
        headers=_sig(created["webhook_secret"], body),
    )
    assert hook.status_code == 202, hook.text
    bad = client.post(f"/v1/webhooks/github/{created['id']}", content=body, headers=_sig("x", body))
    assert bad.status_code == 401


def test_legacy_rows_still_open_and_are_resealed_lazily_and_by_the_command(
    world: World, admin_engine: Engine, deps: SyncDeps, fake_gh: FakeGitHub, base_settings: Settings
) -> None:
    first = _create(world, world.org_id, world.ws, world.ws_admin)
    _to_legacy(admin_engine, base_settings, first["id"])
    assert _account(admin_engine, first["id"]).key_id == "k1"
    assert run(deps, world, first["id"]) == "succeeded"  # dual read
    assert set(fake_gh.auth_seen) == {f"Bearer {CANARY}"}
    assert _account(admin_engine, first["id"]).key_id.startswith("dek:")  # lazy re-seal
    # the migration command: dry run counts, real run re-seals, rerun is a no-op
    _to_legacy(admin_engine, base_settings, first["id"])
    provider = provider_from_settings(base_settings)
    legacy = crypto.cipher_from_settings(base_settings)
    dry = ops.reencrypt(admin_engine, provider, legacy, org=world.org_id, dry_run=True)
    assert dry.counts.get("resealed") == 1
    assert _account(admin_engine, first["id"]).key_id == "k1"
    done = ops.reencrypt(admin_engine, provider, legacy, org=world.org_id, batch=1)
    assert done.counts.get("resealed") == 1
    assert done.failed == 0
    again = ops.reencrypt(admin_engine, provider, legacy, org=world.org_id)
    assert again.counts.get("resealed", 0) == 0
    check = ops.verify(admin_engine, provider, legacy, org=world.org_id)
    assert check.failed == 0
    assert check.counts.get("values_legacy", 0) == 0


def test_tenant_a_cannot_open_tenant_b_secrets(
    world: World, admin_engine: Engine, worker_engine: Engine, base_settings: Settings
) -> None:
    a = _create(world, world.org_id, world.ws, world.ws_admin)
    b = _create(world, world.foreign.org_id, world.foreign_ws, world.foreign.owner)
    acc_b = _account(admin_engine, b["id"])
    provider = provider_from_settings(base_settings)
    # under org A's RLS context, B's data key is invisible
    with worker_session(worker_engine, world.org_id) as session:
        cipher_a = TenantCipher(session, world.org_id, provider)
        with pytest.raises(DecryptionFailed):
            cipher_a.open(bytes(acc_b.token_ciphertext), acc_b.key_id, aad=acc_b.id.bytes)
        assert session.execute(text("SELECT count(*) FROM tenant_keys")).scalar() == 1
    # even holding A's raw DEK (the admin path, no RLS), B's ciphertext does not open
    with admin_engine.connect() as conn:
        dek_a = load_active_dek(conn, world.org_id, provider, create=False)
        assert dek_a is not None
        forged = f"dek:{dek_a.id}"
        with pytest.raises(DecryptionFailed):
            TenantCipher(conn, world.org_id, provider).open(
                bytes(acc_b.token_ciphertext), forged, aad=acc_b.id.bytes
            )
        with pytest.raises(DecryptionFailed):  # org filter: A's cipher never loads B's key
            TenantCipher(conn, world.org_id, provider).open(
                bytes(acc_b.token_ciphertext), acc_b.key_id, aad=acc_b.id.bytes
            )
        acc_a = _account(admin_engine, a["id"])
        own = TenantCipher(conn, world.org_id, provider)
        assert own.open(bytes(acc_a.token_ciphertext), acc_a.key_id, aad=acc_a.id.bytes) == CANARY


def test_kek_rotation_and_new_dek_keep_data_readable(
    world: World,
    admin_engine: Engine,
    deps: SyncDeps,
    base_settings: Settings,
    tmp_path: Path,
) -> None:
    created = _create(world, world.org_id, world.ws, world.ws_admin)
    new_file = tmp_path / "kek2.key"
    new_file.write_text(KEK2, encoding="utf-8")
    rotated = base_settings.model_copy(
        update={
            "MASTER_KEY_FILE": str(new_file),
            "MASTER_KEY_ID": "kek2",
            "MASTER_OLD_KEYS": SecretStr(f"kek1:{KEK1}"),
        }
    )
    p2 = provider_from_settings(rotated)
    assert ops.rotate_kek(admin_engine, p2, dry_run=True, batch=50).counts.get("rewrapped", 0) >= 1
    done = ops.rotate_kek(admin_engine, p2, dry_run=False, batch=1)
    assert done.failed == 0
    assert done.counts["rewrapped"] >= 1
    assert ops.rotate_kek(admin_engine, p2, dry_run=False, batch=50).counts == {}  # idempotent
    # only the new KEK is needed now
    only_new = rotated.model_copy(update={"MASTER_OLD_KEYS": None})
    p_new = provider_from_settings(only_new)
    legacy = crypto.cipher_from_settings(only_new)
    assert ops.verify(admin_engine, p_new, legacy, org=world.org_id).failed == 0
    new_deps = SyncDeps(
        deps.engine,
        deps.settings.model_copy(
            update={"MASTER_KEY_FILE": str(new_file), "MASTER_KEY_ID": "kek2"}
        ),
        deps.store,
        deps.connector_factory,
    )
    assert run(new_deps, world, created["id"]) == "succeeded"
    # new DEK for the org: old rows open through the retired key; re-encryption moves them
    before = _account(admin_engine, created["id"]).key_id
    with admin_engine.begin() as conn:
        new_id = issue_new_dek(conn, world.org_id, p_new)
    assert (
        scalar(admin_engine, "SELECT state FROM tenant_keys WHERE id = :i", i=uuid.UUID(before[4:]))
        == "retired"
    )
    assert ops.verify(admin_engine, p_new, legacy, org=world.org_id).failed == 0
    moved = ops.reencrypt(admin_engine, p_new, legacy, org=world.org_id, batch=1)
    assert moved.failed == 0
    assert moved.counts["resealed"] == 2
    assert moved.counts["batches"] == 2
    acc = _account(admin_engine, created["id"])
    assert acc.key_id == acc.webhook_key_id == f"dek:{new_id}"
    assert run(new_deps, world, created["id"]) in ("succeeded", "skipped")


def test_crypto_shred_makes_the_org_secrets_unrecoverable(
    world: World, admin_engine: Engine, deps: SyncDeps, base_settings: Settings
) -> None:
    created = _create(world, world.org_id, world.ws, world.ws_admin)
    provider = provider_from_settings(base_settings)
    with admin_engine.begin() as conn:
        assert shred_org_keys(conn, world.org_id) == 1
    acc = _account(admin_engine, created["id"])
    with admin_engine.connect() as conn, pytest.raises(DecryptionFailed):
        TenantCipher(conn, world.org_id, provider).open(
            bytes(acc.token_ciphertext), acc.key_id, aad=acc.id.bytes
        )
    assert run(deps, world, created["id"]) == "failed"
    err = scalar(
        admin_engine,
        "SELECT error_code FROM source_syncs WHERE source_id = :s ORDER BY created_at DESC LIMIT 1",
        s=uuid.UUID(created["id"]),
    )
    assert err == "secret_unavailable"
    report = ops.verify(admin_engine, provider, None, org=world.org_id)
    assert report.failed == 2  # token + webhook secret


def test_key_material_never_reaches_logs_audit_or_api(
    world: World,
    admin_engine: Engine,
    deps: SyncDeps,
    base_settings: Settings,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    created = _create(world, world.org_id, world.ws, world.ws_admin)
    _to_legacy(admin_engine, base_settings, created["id"])
    assert run(deps, world, created["id"]) == "succeeded"
    provider = provider_from_settings(base_settings)
    legacy = crypto.cipher_from_settings(base_settings)
    with admin_engine.begin() as conn:
        issue_new_dek(conn, world.org_id, provider)
    reports = [
        ops.reencrypt(admin_engine, provider, legacy, org=world.org_id),
        ops.verify(admin_engine, provider, legacy),
    ]
    deks = [
        provider.unwrap(bytes(k.wrapped_key), k.kek_id, context=wrap_context(k.org_id, k.id))
        for k in rows(admin_engine, "SELECT * FROM tenant_keys WHERE org_id = :o", o=world.org_id)
    ]
    assert len(deks) == 2
    listing = world.api.get(world.path("/sources"), world.ws_admin)
    assert listing.status == 200
    audit = rows(admin_engine, "SELECT to_jsonb(a)::text AS j FROM audit_events a")
    haystack = "\n".join(
        [
            caplog.text,
            capsys.readouterr().out,
            str(listing.body),
            *(r.j for r in audit),
            *(r.line() for r in reports),
            repr(provider),
            repr(legacy),
        ]
    )
    assert base_settings.CONNECTOR_ENCRYPTION_KEY is not None
    legacy_key = base_settings.CONNECTOR_ENCRYPTION_KEY.get_secret_value()
    needles = [CANARY, KEK1, base64.b64decode(KEK1).hex(), legacy_key]
    for dek in deks:
        needles += [base64.b64encode(dek).decode(), dek.hex()]
    for needle in needles:
        assert needle not in haystack
