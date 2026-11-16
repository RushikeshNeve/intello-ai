"""M01.01.01 unit tests: forged tokens against a fake JWKS (no network)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime

import httpx
import jwt
import pytest

from app.core.security import InvalidTokenError, TokenVerifier
from app.modules.identity import oidc
from app.modules.identity.oidc import MIN_FORCED_REFRESH_SECONDS, OidcTokenVerifier
from tests.identity.jwt_factory import (
    AUDIENCE,
    INTERNAL,
    ISSUER,
    SUBJECT,
    FakeClock,
    FakeJwks,
    RsaKey,
    b64url,
    claims,
    settings,
    unsigned_token,
)


@pytest.fixture(scope="module")
def key() -> RsaKey:
    return RsaKey("key-1")


@pytest.fixture(scope="module")
def other_key() -> RsaKey:
    return RsaKey("key-2")


@pytest.fixture
def jwks(key: RsaKey) -> FakeJwks:
    return FakeJwks(key.jwk())


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def verifier(jwks: FakeJwks, clock: FakeClock) -> OidcTokenVerifier:
    return OidcTokenVerifier(settings(), jwks.client(), monotonic=clock)


def _reason(verifier: OidcTokenVerifier, token: str) -> str:
    with pytest.raises(InvalidTokenError) as info:
        verifier.verify(token)
    assert info.value.reason in oidc.REASONS
    return info.value.reason


# -- valid ---------------------------------------------------------------------------------
def test_valid_token_returns_claims(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    payload = claims()
    result = verifier.verify(key.sign(payload))
    assert result.issuer == ISSUER
    assert result.subject == SUBJECT
    assert result.email == "alice@ukos.test"
    assert result.email_verified is True
    assert result.name == "alice"
    assert result.audience == (AUDIENCE, "ukos-web")
    assert result.expires_at == datetime.fromtimestamp(payload["exp"], UTC)
    assert result.raw["azp"] == "ukos-web"
    assert isinstance(verifier, TokenVerifier)


def test_string_audience_and_optional_profile_claims(
    verifier: OidcTokenVerifier, key: RsaKey
) -> None:
    result = verifier.verify(
        key.sign(claims(aud=AUDIENCE, email=None, name=None, email_verified=None))
    )
    assert result.audience == (AUDIENCE,)
    assert result.email is None
    assert result.name is None
    assert result.email_verified is False


def test_within_leeway_is_accepted(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    now = int(time.time())
    # Expired 10 s ago and issued 10 s in the future: both inside the 30 s leeway.
    verifier.verify(key.sign(claims(exp=now - 10, iat=now + 10, nbf=now + 10)))


# -- claim failures ------------------------------------------------------------------------
def test_wrong_issuer(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    assert _reason(verifier, key.sign(claims(iss="http://evil.test/dex"))) == "bad_issuer"


@pytest.mark.parametrize(
    "iss", [ISSUER + "/", ISSUER.upper(), "http://127.0.0.1:28381/de", ISSUER + "x", ""]
)
def test_issuer_must_match_exactly(verifier: OidcTokenVerifier, key: RsaKey, iss: str) -> None:
    assert _reason(verifier, key.sign(claims(iss=iss))) == "bad_issuer"


def test_issuer_as_list_is_rejected(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    assert _reason(verifier, key.sign_raw(claims(iss=[ISSUER]))) in {"bad_issuer", "malformed"}


def test_internal_url_is_not_an_accepted_issuer(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    assert _reason(verifier, key.sign(claims(iss=INTERNAL))) == "bad_issuer"


@pytest.mark.parametrize("aud", ["ukos-web", ["ukos-web"], ["ukos-api-x"], []])
def test_wrong_audience(verifier: OidcTokenVerifier, key: RsaKey, aud: object) -> None:
    assert _reason(verifier, key.sign(claims(aud=aud))) in {"bad_audience", "missing_claim"}


def test_wrong_audience_single(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    assert _reason(verifier, key.sign(claims(aud=["ukos-web"]))) == "bad_audience"


def test_expired(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    now = int(time.time())
    assert _reason(verifier, key.sign(claims(exp=now - 31, iat=now - 600))) == "expired"


def test_expired_with_zero_leeway(jwks: FakeJwks, clock: FakeClock, key: RsaKey) -> None:
    strict = OidcTokenVerifier(settings(OIDC_LEEWAY_SECONDS=0), jwks.client(), monotonic=clock)
    now = int(time.time())
    assert _reason(strict, key.sign(claims(exp=now - 2, iat=now - 60))) == "expired"


def test_nbf_in_future_beyond_leeway(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    now = int(time.time())
    assert _reason(verifier, key.sign(claims(nbf=now + 120))) == "not_yet_valid"


def test_iat_in_future_beyond_leeway(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    now = int(time.time())
    assert _reason(verifier, key.sign(claims(iat=now + 120, exp=now + 900))) == "not_yet_valid"


@pytest.mark.parametrize("missing", ["sub", "exp", "iat", "iss", "aud"])
def test_required_claims(verifier: OidcTokenVerifier, key: RsaKey, missing: str) -> None:
    payload = claims()
    del payload[missing]
    assert _reason(verifier, key.sign(payload)) == "missing_claim"


@pytest.mark.parametrize("sub", ["", "   ", 123])
def test_empty_or_non_string_sub(verifier: OidcTokenVerifier, key: RsaKey, sub: object) -> None:
    assert _reason(verifier, key.sign(claims(sub=sub))) in {"missing_claim", "malformed"}


@pytest.mark.parametrize("field", ["exp", "iat"])
def test_non_numeric_times(verifier: OidcTokenVerifier, key: RsaKey, field: str) -> None:
    assert _reason(verifier, key.sign(claims(**{field: "soon"}))) == "malformed"


# -- algorithm / signature -----------------------------------------------------------------
def test_alg_none(verifier: OidcTokenVerifier) -> None:
    token = unsigned_token({"alg": "none", "kid": "key-1", "typ": "JWT"}, claims())
    assert _reason(verifier, token) == "unsupported_alg"


@pytest.mark.parametrize("alg", ["None", "NONE", "", "RS384", "RS512", "PS256", "ES256", "EdDSA"])
def test_other_algorithms(verifier: OidcTokenVerifier, alg: str) -> None:
    token = unsigned_token({"alg": alg, "kid": "key-1"}, claims(), b"sig")
    assert _reason(verifier, token) == "unsupported_alg"


def test_missing_alg(verifier: OidcTokenVerifier) -> None:
    assert _reason(verifier, unsigned_token({"kid": "key-1"}, claims(), b"x")) == "unsupported_alg"


@pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512"])
def test_hmac_with_public_key_as_secret(
    verifier: OidcTokenVerifier, jwks: FakeJwks, key: RsaKey, alg: str
) -> None:
    """Algorithm confusion: an HMAC token keyed with the RSA public key (PEM) must not pass."""
    header = {"alg": alg, "kid": "key-1", "typ": "JWT"}
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims()).encode())}"
    digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[alg]
    sig = hmac.new(key.public_pem(), signing_input.encode(), digest).digest()
    token = f"{signing_input}.{b64url(sig)}"
    assert _reason(verifier, token) == "unsupported_alg"
    assert jwks.calls == 0  # rejected before any key lookup


def test_tampered_payload(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    header, _, sig = key.sign(claims()).split(".")
    forged = b64url(json.dumps(claims(sub="mallory", email="mallory@ukos.test")).encode())
    assert _reason(verifier, f"{header}.{forged}.{sig}") == "bad_signature"


def test_signed_by_other_key_with_known_kid(verifier: OidcTokenVerifier, other_key: RsaKey) -> None:
    forged = jwt.encode(claims(), other_key.private, algorithm="RS256", headers={"kid": "key-1"})
    assert _reason(verifier, forged) == "bad_signature"


def test_stripped_signature(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    header, payload, _ = key.sign(claims()).split(".")
    assert _reason(verifier, f"{header}.{payload}.") in {"bad_signature", "malformed"}


def test_jwk_alg_must_match_header(clock: FakeClock, key: RsaKey) -> None:
    """A key published for another alg is never used for an RS256 token."""
    jwks = FakeJwks(key.jwk(alg="RS512"))
    verifier = OidcTokenVerifier(settings(), jwks.client(), monotonic=clock)
    assert _reason(verifier, key.sign(claims())) == "unknown_kid"


@pytest.mark.parametrize(
    "token", ["", "abc", "a.b", "a.b.c", "....", "x" * (oidc.MAX_TOKEN_BYTES + 1)]
)
def test_malformed(verifier: OidcTokenVerifier, token: str) -> None:
    assert _reason(verifier, token) == "malformed"


def test_missing_kid(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    token = jwt.encode(claims(), key.private, algorithm="RS256")
    assert _reason(verifier, token) == "malformed"


# -- JWKS caching / unknown kid ------------------------------------------------------------
def test_jwks_cached_between_tokens(
    verifier: OidcTokenVerifier, jwks: FakeJwks, key: RsaKey, clock: FakeClock
) -> None:
    for _ in range(5):
        verifier.verify(key.sign(claims()))
    assert jwks.calls == 1
    clock.now += 299
    verifier.verify(key.sign(claims()))
    assert jwks.calls == 1
    clock.now += 2  # past OIDC_JWKS_CACHE_SECONDS
    verifier.verify(key.sign(claims()))
    assert jwks.calls == 2


def test_unknown_kid_one_refetch_then_fail(
    verifier: OidcTokenVerifier, jwks: FakeJwks, key: RsaKey, other_key: RsaKey, clock: FakeClock
) -> None:
    verifier.verify(key.sign(claims()))
    assert jwks.calls == 1
    clock.now += MIN_FORCED_REFRESH_SECONDS
    unknown = other_key.sign(claims())
    assert _reason(verifier, unknown) == "unknown_kid"
    assert jwks.calls == 2  # exactly one refetch
    for _ in range(5):
        assert _reason(verifier, unknown) == "unknown_kid"
    clock.now += MIN_FORCED_REFRESH_SECONDS * 3
    assert _reason(verifier, unknown) == "unknown_kid"
    assert jwks.calls == 2  # the same kid never triggers another early refetch


def test_unknown_kid_flood_is_rate_limited(
    verifier: OidcTokenVerifier, jwks: FakeJwks, key: RsaKey, clock: FakeClock
) -> None:
    verifier.verify(key.sign(claims()))
    clock.now += MIN_FORCED_REFRESH_SECONDS
    for i in range(50):
        forged = RsaKey(f"forged-{i}") if i < 2 else None
        token = (
            forged.sign(claims())
            if forged
            else unsigned_token({"alg": "RS256", "kid": f"forged-{i}"}, claims(), b"x")
        )
        assert _reason(verifier, token) == "unknown_kid"
    assert jwks.calls == 2  # one early refetch in the whole window
    clock.now += MIN_FORCED_REFRESH_SECONDS
    _reason(verifier, unsigned_token({"alg": "RS256", "kid": "forged-new"}, claims(), b"x"))
    assert jwks.calls == 3


def test_key_rotation_picked_up_by_refetch(
    verifier: OidcTokenVerifier, jwks: FakeJwks, key: RsaKey, other_key: RsaKey, clock: FakeClock
) -> None:
    verifier.verify(key.sign(claims()))
    jwks.keys.append(other_key.jwk())  # IdP rotates in a new signing key
    clock.now += MIN_FORCED_REFRESH_SECONDS
    assert verifier.verify(other_key.sign(claims())).subject == SUBJECT
    assert jwks.calls == 2


def test_unknown_kid_on_first_fetch_does_not_refetch(
    verifier: OidcTokenVerifier, jwks: FakeJwks, other_key: RsaKey
) -> None:
    assert _reason(verifier, other_key.sign(claims())) == "unknown_kid"
    assert jwks.calls == 1  # the fetch that just happened is already fresh


def test_non_signing_and_foreign_keys_are_ignored(clock: FakeClock, key: RsaKey) -> None:
    jwks = FakeJwks(
        key.jwk(use="enc"),
        {"kty": "oct", "kid": "key-1", "k": b64url(b"secret"), "alg": "HS256"},
        {"kty": "RSA", "kid": "broken", "n": "AQAB", "e": "AQAB"},
        "not-a-dict",  # type: ignore[arg-type]
    )
    verifier = OidcTokenVerifier(settings(), jwks.client(), monotonic=clock)
    assert _reason(verifier, key.sign(claims())) == "unknown_kid"


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
)
def test_jwks_unreachable(
    jwks: FakeJwks, clock: FakeClock, key: RsaKey, failure: Exception
) -> None:
    jwks.fail = failure
    verifier = OidcTokenVerifier(settings(), jwks.client(), monotonic=clock)
    assert _reason(verifier, key.sign(claims())) == "jwks_unavailable"
    assert _reason(verifier, key.sign(claims())) == "jwks_unavailable"
    assert jwks.calls == 1  # failures are retried after a delay, not on every request
    clock.now += oidc.FETCH_RETRY_SECONDS
    jwks.fail = None
    assert verifier.verify(key.sign(claims())).subject == SUBJECT
    assert jwks.calls == 2


@pytest.mark.parametrize("status", [404, 500, 503])
def test_jwks_http_error(jwks: FakeJwks, clock: FakeClock, key: RsaKey, status: int) -> None:
    jwks.status = status
    verifier = OidcTokenVerifier(settings(), jwks.client(), monotonic=clock)
    assert _reason(verifier, key.sign(claims())) == "jwks_unavailable"


def test_jwks_invalid_document(clock: FakeClock, key: RsaKey) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    verifier = OidcTokenVerifier(settings(), client, monotonic=clock)
    assert _reason(verifier, key.sign(claims())) == "jwks_unavailable"


def test_stale_keys_served_when_refresh_fails(
    verifier: OidcTokenVerifier, jwks: FakeJwks, key: RsaKey, clock: FakeClock
) -> None:
    verifier.verify(key.sign(claims()))
    clock.now += 301
    jwks.fail = httpx.ConnectError("down")
    assert verifier.verify(key.sign(claims())).subject == SUBJECT
    assert verifier.verify(key.sign(claims())).subject == SUBJECT
    assert jwks.calls == 2  # one failed refresh, then back off


def test_jwks_url_uses_internal_url(verifier: OidcTokenVerifier, key: RsaKey) -> None:
    # FakeJwks.handler asserts the URL is OIDC_INTERNAL_URL + "/keys".
    verifier.verify(key.sign(claims()))


# -- logging -------------------------------------------------------------------------------
def test_tokens_never_logged(
    verifier: OidcTokenVerifier, jwks: FakeJwks, key: RsaKey, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    tokens = [
        key.sign(claims()),
        key.sign(claims(iss="http://evil.test")),
        unsigned_token({"alg": "none", "kid": "key-1"}, claims()),
    ]
    messages = []
    for token in tokens:
        try:
            verifier.verify(token)
        except InvalidTokenError as exc:
            messages.append(str(exc))
    assert messages == ["bad_issuer", "unsupported_alg"]
    jwks.fail = httpx.ConnectError("down")
    for token in tokens:
        assert token not in caplog.text
        for part in token.split("."):
            if len(part) > 8:
                assert part not in caplog.text
