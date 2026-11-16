"""Through ``create_app``: real Dex token -> OidcTokenVerifier -> DbUserProvisioner -> /v1/me."""

from __future__ import annotations

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
def client(dex: Dex, app_db_url: URL) -> Iterator[TestClient]:
    settings = Settings(
        DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)),
        OIDC_ISSUER_URL=dex.issuer,
        OIDC_INTERNAL_URL=dex.issuer,
        OIDC_AUDIENCE="ukos-api",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
    dispose_engines()


def test_me_provisions_on_first_request_and_is_idempotent(
    client: TestClient, dex: Dex, admin_engine: Engine
) -> None:
    token = dex.token("bob@ukos.test", "ukos-dev-bob-password")
    first = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["email"] == "bob@ukos.test"
    assert body["display_name"] == "bob"

    again = client.get(
        "/v1/me",
        headers={"Authorization": f"Bearer {dex.token('bob@ukos.test', 'ukos-dev-bob-password')}"},
    )
    assert again.json() == body

    with admin_engine.connect() as conn:
        users = conn.execute(
            text("SELECT count(*) FROM users WHERE issuer = :i AND email = 'bob@ukos.test'"),
            {"i": dex.issuer},
        ).scalar_one()
        events = conn.execute(
            text(
                "SELECT count(*) FROM audit_events"
                " WHERE event_type = 'user.provisioned' AND actor_user_id = :u"
            ),
            {"u": body["id"]},
        ).scalar_one()
    assert (users, events) == (1, 1)


def test_me_rejects_real_tokens_that_fail_validation(
    client: TestClient, dex: Dex, dex_short_ttl: Dex, admin_engine: Engine
) -> None:
    with admin_engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
    for token in (
        dex.token(audience=None),  # no ukos-api audience
        dex_short_ttl.token(),  # other issuer and keys
        "Bearer-less garbage",
    ):
        res = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 401
        assert res.json() == {"detail": "invalid_token"}
        assert res.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert client.get("/v1/me").status_code == 401
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == before
