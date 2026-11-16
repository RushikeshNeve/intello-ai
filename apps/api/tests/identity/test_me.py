"""``get_current_principal`` end to end via ``create_app``: real verifier, fake JWKS/provisioner."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.security import (
    INVALID_TOKEN,
    WWW_AUTHENTICATE_INVALID_TOKEN,
    Principal,
    VerifiedClaims,
    get_current_principal,
    get_token_verifier,
    get_user_provisioner,
)
from app.main import _db_user_provisioner, create_app
from app.modules.identity.oidc import OidcTokenVerifier
from app.modules.identity.provisioning import DbUserProvisioner
from tests.identity.jwt_factory import SUBJECT, FakeJwks, RsaKey, b64url, claims, settings

USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


class RecordingProvisioner:
    def __init__(self) -> None:
        self.seen: list[VerifiedClaims] = []

    def provision(self, claims: VerifiedClaims) -> Principal:
        self.seen.append(claims)
        return Principal(
            user_id=USER_ID,
            issuer=claims.issuer,
            subject=claims.subject,
            email=claims.email,
            display_name=claims.name,
        )


@pytest.fixture(scope="module")
def key() -> RsaKey:
    return RsaKey("k1")


@pytest.fixture
def harness(key: RsaKey) -> tuple[TestClient, RecordingProvisioner, FakeJwks]:
    cfg = settings()
    app = create_app(cfg)
    jwks = FakeJwks(key.jwk())
    verifier = OidcTokenVerifier(cfg, jwks.client())
    provisioner = RecordingProvisioner()
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    app.dependency_overrides[get_user_provisioner] = lambda: provisioner
    return TestClient(app), provisioner, jwks


def _assert_401(res: object) -> None:
    assert res.status_code == 401  # type: ignore[attr-defined]
    assert res.json() == {"detail": INVALID_TOKEN}  # type: ignore[attr-defined]
    assert res.headers["www-authenticate"] == WWW_AUTHENTICATE_INVALID_TOKEN  # type: ignore[attr-defined]


def test_me_with_valid_token(
    harness: tuple[TestClient, RecordingProvisioner, FakeJwks], key: RsaKey
) -> None:
    client, provisioner, _ = harness
    res = client.get("/v1/me", headers={"Authorization": f"Bearer {key.sign(claims())}"})
    assert res.status_code == 200
    assert res.json() == {"id": str(USER_ID), "email": "alice@ukos.test", "display_name": "alice"}
    assert [c.subject for c in provisioner.seen] == [SUBJECT]


def test_me_without_token(harness: tuple[TestClient, RecordingProvisioner, FakeJwks]) -> None:
    client, provisioner, jwks = harness
    _assert_401(client.get("/v1/me"))
    assert provisioner.seen == []
    assert jwks.calls == 0


def _bad_tokens(key: RsaKey, other: RsaKey) -> dict[str, str]:
    now = int(time.time())
    header, _, sig = key.sign(claims()).split(".")
    return {
        "wrong_issuer": key.sign(claims(iss="http://evil.test/dex")),
        "wrong_audience": key.sign(claims(aud=["ukos-web"])),
        "expired": key.sign(claims(exp=now - 120, iat=now - 900)),
        "future_nbf": key.sign(claims(nbf=now + 300)),
        "alg_none": f"{b64url(b'{"alg":"none","kid":"k1"}')}.{b64url(b'{}')}.",
        "unknown_kid": other.sign(claims()),
        "tampered": f"{header}.{b64url(b'{"sub":"mallory"}')}.{sig}",
        "garbage": "not-a-jwt",
    }


@pytest.mark.parametrize(
    "case",
    [
        "wrong_issuer",
        "wrong_audience",
        "expired",
        "future_nbf",
        "alg_none",
        "unknown_kid",
        "tampered",
        "garbage",
    ],
)
def test_me_with_invalid_token_is_401(
    harness: tuple[TestClient, RecordingProvisioner, FakeJwks],
    key: RsaKey,
    case: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client, provisioner, _ = harness
    token = _bad_tokens(key, RsaKey("other"))[case]
    res = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    _assert_401(res)
    assert provisioner.seen == []
    assert token not in caplog.text
    assert token not in res.text


def test_principal_dependency_for_other_routes(
    harness: tuple[TestClient, RecordingProvisioner, FakeJwks], key: RsaKey
) -> None:
    client, _, _ = harness
    app = cast(FastAPI, client.app)

    @app.get("/v1/_probe")
    async def probe(principal: Annotated[Principal, Depends(get_current_principal)]) -> str:
        return principal.subject

    res = client.get("/v1/_probe", headers={"Authorization": f"Bearer {key.sign(claims())}"})
    assert res.json() == SUBJECT


def test_create_app_wires_identity_implementations() -> None:
    app = create_app(settings())
    verifier = app.dependency_overrides[get_token_verifier]()
    assert isinstance(verifier, OidcTokenVerifier)
    assert app.dependency_overrides[get_token_verifier]() is verifier  # one shared JWKS cache
    assert app.dependency_overrides[get_user_provisioner] is _db_user_provisioner
    assert isinstance(_db_user_provisioner(engine=object()), DbUserProvisioner)  # type: ignore[arg-type]


def test_unwired_defaults_still_map_to_500() -> None:
    app = create_app(settings())
    app.dependency_overrides.pop(get_token_verifier)
    app.dependency_overrides.pop(get_user_provisioner)
    res = TestClient(app, raise_server_exceptions=False).get(
        "/v1/me", headers={"Authorization": "Bearer a.b.c"}
    )
    assert res.status_code == 500
    assert res.json() == {"detail": "internal_error"}
