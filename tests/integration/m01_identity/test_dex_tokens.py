"""M01.01.01 on real Dex: valid tokens pass; wrong issuer, audience and expired are rejected."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import jwt
import pytest
from m01_identity_support import DEX_EXP_TTL_SECONDS, Dex

from app.core.config import Settings
from app.core.security import InvalidTokenError
from app.modules.identity.oidc import OidcTokenVerifier

ALICE_SUB_PREFIX = "CiQ2YjFjNGYwZS0xYTUyLTRjOGUtOWEwZC0wMDAwMDAwMDAwMDE"  # base64 of alice's userID


def _verifier(dex: Dex, **overrides: object) -> OidcTokenVerifier:
    values: dict[str, object] = {
        "OIDC_ISSUER_URL": dex.issuer,
        # Host tests reach Dex on the published port (in compose this is http://dex:5556/dex).
        "OIDC_INTERNAL_URL": dex.issuer,
        "OIDC_AUDIENCE": "ukos-api",
        "OIDC_LEEWAY_SECONDS": 30,
    }
    values.update(overrides)
    return OidcTokenVerifier(Settings(**values))  # type: ignore[arg-type]


def _reason(verifier: OidcTokenVerifier, token: str) -> str:
    with pytest.raises(InvalidTokenError) as info:
        verifier.verify(token)
    return info.value.reason


@pytest.mark.parametrize("kind", ["id_token", "access_token"])
def test_valid_dex_token_accepted(dex: Dex, kind: str) -> None:
    token = dex.token(kind=kind)
    claims = _verifier(dex).verify(token)
    assert claims.issuer == dex.issuer
    assert claims.subject.startswith(ALICE_SUB_PREFIX)
    assert claims.email == "alice@ukos.test"
    assert claims.email_verified is True
    assert claims.name == "alice"
    assert "ukos-api" in claims.audience
    assert claims.expires_at > datetime.now(UTC)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_each_test_user_has_a_distinct_subject(dex: Dex) -> None:
    verifier = _verifier(dex)
    subjects = {
        verifier.verify(dex.token(f"{u}@ukos.test", f"ukos-dev-{u}-password")).subject
        for u in ("alice", "bob", "mallory")
    }
    assert len(subjects) == 3


def test_real_token_wrong_expected_issuer(dex: Dex) -> None:
    token = dex.token()
    for issuer in (
        dex.issuer.replace("127.0.0.1", "localhost"),
        dex.issuer + "/",
        "http://dex:5556/dex",
    ):
        assert _reason(_verifier(dex, OIDC_ISSUER_URL=issuer), token) == "bad_issuer"


def test_real_token_without_api_audience(dex: Dex) -> None:
    token = dex.token(audience=None)
    assert jwt.decode(token, options={"verify_signature": False})["aud"] == "ukos-web"
    assert _reason(_verifier(dex), token) == "bad_audience"


def test_real_token_for_other_expected_audience(dex: Dex) -> None:
    assert _reason(_verifier(dex, OIDC_AUDIENCE="other-api"), dex.token()) == "bad_audience"


def test_real_token_tampered(dex: Dex) -> None:
    header, payload, sig = dex.token().split(".")
    other = dex.token("mallory@ukos.test", "ukos-dev-mallory-password").split(".")[1]
    assert _reason(_verifier(dex), f"{header}.{other}.{sig}") == "bad_signature"
    assert _reason(_verifier(dex), f"{header}.{payload}.{sig[:-4]}AAAA") == "bad_signature"


def test_real_token_from_another_dex_instance(dex: Dex, dex_short_ttl: Dex) -> None:
    """Different issuer *and* signing keys: rejected even when the issuer is trusted."""
    foreign = dex_short_ttl.token()
    assert _reason(_verifier(dex), foreign) == "unknown_kid"
    verifier = _verifier(dex, OIDC_ISSUER_URL=dex_short_ttl.issuer)  # keys still from `dex`
    assert _reason(verifier, foreign) == "unknown_kid"


def test_real_expired_token(dex_short_ttl: Dex) -> None:
    token = dex_short_ttl.token()
    verifier = _verifier(dex_short_ttl, OIDC_LEEWAY_SECONDS=0)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == DEX_EXP_TTL_SECONDS
    remaining = claims["exp"] - time.time()
    if remaining > 0.5:
        # Fresh enough to be valid before expiry: proves the rejection below is about time only.
        assert verifier.verify(token).subject == claims["sub"]
    time.sleep(max(0.0, claims["exp"] - time.time()) + 1.5)
    assert _reason(verifier, token) == "expired"
    # Inside the configured leeway the same token is still accepted.
    lenient = _verifier(dex_short_ttl, OIDC_LEEWAY_SECONDS=30)
    assert lenient.verify(token).subject == claims["sub"]
