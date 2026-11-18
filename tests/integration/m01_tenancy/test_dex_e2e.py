"""Real Dex end to end: alice creates an org, adds bob, bob sees it, mallory gets 404.

Everything is real: Dex tokens -> ``OidcTokenVerifier`` -> ``DbUserProvisioner`` -> tenancy routes
on the app role under RLS. Dex runs from ``identity.yml`` (pinned ``v2.45.1``) under project
``ukos-m01-tenancy-dex`` on host port 28391.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from m01_identity_support import Dex
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL

from app.core.config import Settings
from app.core.db import dispose_engines
from app.main import create_app


@pytest.fixture
def dex_client(dex: Dex, app_db_url: URL) -> Iterator[TestClient]:
    settings = Settings(
        DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)),
        OIDC_ISSUER_URL=dex.issuer,
        OIDC_INTERNAL_URL=dex.issuer,
        OIDC_AUDIENCE="ukos-api",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
    dispose_engines()


def _auth(dex: Dex, who: str) -> dict[str, str]:
    token = dex.token(f"{who}@ukos.test", f"ukos-dev-{who}-password")
    return {"Authorization": f"Bearer {token}"}


def test_alice_creates_org_adds_bob_and_mallory_is_kept_out(
    dex_client: TestClient, dex: Dex, admin_engine: Engine
) -> None:
    alice, bob, mallory = (_auth(dex, u) for u in ("alice", "bob", "mallory"))

    # bob must have signed in once (users are provisioned on first request; no invites yet).
    bob_me = dex_client.get("/v1/me", headers=bob)
    assert bob_me.status_code == 200, bob_me.text
    bob_id = bob_me.json()["id"]

    slug = f"dex-{uuid.uuid4().hex[:10]}"
    created = dex_client.post("/v1/orgs", headers=alice, json={"slug": slug, "name": "Alice Co"})
    assert created.status_code == 201, created.text
    org = created.json()
    assert (org["slug"], org["role"]) == (slug, "owner")

    added = dex_client.put(
        f"/v1/orgs/{org['id']}/members/{bob_id}", headers=alice, json={"role": "member"}
    )
    assert added.status_code == 200, added.text
    assert added.json() == {
        "user_id": bob_id,
        "email": "bob@ukos.test",
        "display_name": "bob",
        "role": "member",
    }

    bob_orgs = dex_client.get("/v1/orgs", headers=bob)
    assert bob_orgs.status_code == 200
    assert {"id": org["id"], "slug": slug, "name": "Alice Co", "role": "member"} in bob_orgs.json()[
        "items"
    ]
    assert dex_client.get(f"/v1/orgs/{org['id']}", headers=bob).status_code == 200

    for method, path in [
        ("GET", f"/v1/orgs/{org['id']}"),
        ("GET", f"/v1/orgs/{org['id']}/members"),
        ("GET", f"/v1/orgs/{org['id']}/workspaces"),
    ]:
        res = dex_client.request(method, path, headers=mallory)
        assert (res.status_code, res.json()) == (404, {"detail": "not_found"}), path
    assert org["id"] not in {
        o["id"] for o in dex_client.get("/v1/orgs", headers=mallory).json()["items"]
    }

    with admin_engine.connect() as conn:
        events = conn.execute(
            text(
                "SELECT event_type, outcome FROM audit_events WHERE org_id = :o"
                " ORDER BY occurred_at"
            ),
            {"o": org["id"]},
        ).all()
    assert [tuple(e) for e in events] == [
        ("org.created", "success"),
        ("org.member.added", "success"),
        ("authz.denied", "denied"),
        ("authz.denied", "denied"),
        ("authz.denied", "denied"),
    ]
