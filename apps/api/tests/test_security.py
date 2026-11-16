from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import (
    INVALID_TOKEN,
    WWW_AUTHENTICATE_INVALID_TOKEN,
    InvalidTokenError,
    Principal,
    TokenVerifier,
    UserProvisioner,
    VerifiedClaims,
    extract_bearer_token,
    get_current_principal,
    get_token_verifier,
    get_user_provisioner,
)
from app.main import create_app

GOOD_TOKEN = "header.payload.signature"
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class FakeVerifier:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def verify(self, token: str) -> VerifiedClaims:
        self.seen.append(token)
        if token != GOOD_TOKEN:
            raise InvalidTokenError("signature mismatch")
        return VerifiedClaims(
            issuer="http://127.0.0.1:8380/dex",
            subject="alice-sub",
            email="alice@example.test",
            email_verified=True,
            name="Alice",
            audience=("ukos-api",),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            raw={"sub": "alice-sub"},
        )


class FakeProvisioner:
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


def _app() -> tuple[FastAPI, FakeVerifier, FakeProvisioner]:
    app = create_app(Settings())
    verifier, provisioner = FakeVerifier(), FakeProvisioner()

    @app.get("/whoami")
    async def whoami(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> dict[str, str]:
        return {"user_id": str(principal.user_id), "subject": principal.subject}

    app.dependency_overrides[get_token_verifier] = lambda: verifier
    app.dependency_overrides[get_user_provisioner] = lambda: provisioner
    return app, verifier, provisioner


def _assert_invalid_token(res: object) -> None:
    assert res.status_code == 401  # type: ignore[attr-defined]
    assert res.json() == {"detail": INVALID_TOKEN}  # type: ignore[attr-defined]
    assert res.headers["www-authenticate"] == WWW_AUTHENTICATE_INVALID_TOKEN  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Token abc",
        "Bearer two parts",
        GOOD_TOKEN,
    ],
)
def test_missing_or_malformed_header_is_401(header: str | None) -> None:
    app, verifier, provisioner = _app()
    headers = {} if header is None else {"Authorization": header}
    res = TestClient(app).get("/whoami", headers=headers)
    _assert_invalid_token(res)
    assert verifier.seen == []
    assert provisioner.seen == []


def test_verifier_failure_is_401_and_token_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    app, verifier, provisioner = _app()
    res = TestClient(app).get("/whoami", headers={"Authorization": "Bearer forged.token.value"})
    _assert_invalid_token(res)
    assert verifier.seen == ["forged.token.value"]
    assert provisioner.seen == []
    assert "forged.token.value" not in caplog.text
    assert "forged.token.value" not in res.text


def test_valid_token_verifies_then_provisions() -> None:
    app, verifier, provisioner = _app()
    res = TestClient(app).get("/whoami", headers={"Authorization": f"bearer  {GOOD_TOKEN} "})
    assert res.status_code == 200
    assert res.json() == {"user_id": str(USER_ID), "subject": "alice-sub"}
    assert verifier.seen == [GOOD_TOKEN]
    assert [c.subject for c in provisioner.seen] == ["alice-sub"]


def test_unwired_components_map_to_500_without_detail_leak() -> None:
    app = create_app(Settings())

    @app.get("/whoami")
    async def whoami(principal: Annotated[Principal, Depends(get_current_principal)]) -> str:
        return principal.subject

    res = TestClient(app, raise_server_exceptions=False).get(
        "/whoami", headers={"Authorization": f"Bearer {GOOD_TOKEN}"}
    )
    assert res.status_code == 500
    assert res.json() == {"detail": "internal_error"}


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeVerifier(), TokenVerifier)
    assert isinstance(FakeProvisioner(), UserProvisioner)


def test_dataclasses_are_frozen_and_hide_raw_claims() -> None:
    claims = FakeVerifier().verify(GOOD_TOKEN)
    with pytest.raises(AttributeError):
        claims.subject = "mallory"  # type: ignore[misc]
    assert "raw" not in repr(claims)


def test_extract_bearer_token() -> None:
    assert extract_bearer_token("Bearer abc.def") == "abc.def"
    assert extract_bearer_token("BEARER abc") == "abc"


def test_settings_oidc_defaults_and_leeway_cap() -> None:
    settings = Settings()
    assert settings.OIDC_JWKS_CACHE_SECONDS == 300
    assert settings.OIDC_LEEWAY_SECONDS == 30
    assert settings.sqlalchemy_migration_url.startswith("postgresql+psycopg://")
    with pytest.raises(ValueError, match="OIDC_LEEWAY_SECONDS"):
        Settings(OIDC_LEEWAY_SECONDS=31)
