"""OIDC bearer-token verification (M01.01.01, contract docs/architecture/m01-contracts.md §1).

:class:`OidcTokenVerifier` implements :class:`app.core.security.TokenVerifier`:

* **RS256 only.** The header ``alg`` must be ``RS256`` before any key is looked up, so ``none``,
  ``HS*`` (including the HS256-with-public-key confusion attack) and every other algorithm are
  rejected. JWKS entries that are not RSA signing keys for RS256 are ignored, so a token can never
  be checked with a key whose ``alg`` differs from the header's.
* **JWKS** is fetched from ``OIDC_INTERNAL_URL/keys`` (compose-internal URL) and cached for
  ``OIDC_JWKS_CACHE_SECONDS``. A token with an unknown ``kid`` triggers at most one early refetch
  for that ``kid``, and early refetches are globally rate limited
  (:data:`MIN_FORCED_REFRESH_SECONDS`), so forged ``kid`` values can't make us hammer the IdP.
* **Claims:** ``iss == OIDC_ISSUER_URL`` exactly (the public issuer, even though keys come from the
  internal URL); ``OIDC_AUDIENCE`` in ``aud``; ``exp``, ``iat`` and (when present) ``nbf`` checked
  with ``OIDC_LEEWAY_SECONDS``; ``sub`` a non-empty string.

Every failure raises :class:`InvalidTokenError` with one of the stable :data:`REASONS`. The token
itself is never logged or put into an exception message.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import jwt

from app.core.config import Settings
from app.core.security import InvalidTokenError, VerifiedClaims

logger = logging.getLogger("ukos.identity.oidc")

ALGORITHM: Final = "RS256"
# Early (unknown-kid) JWKS refetches are allowed at most once per this many seconds overall.
MIN_FORCED_REFRESH_SECONDS: Final = 10.0
# After a failed JWKS fetch with keys already cached, keep using them and retry after this delay.
FETCH_RETRY_SECONDS: Final = 10.0
# Bearer tokens larger than this are rejected before parsing (cheap DoS guard).
MAX_TOKEN_BYTES: Final = 16 * 1024
_MAX_REMEMBERED_MISSING_KIDS: Final = 1024
_HTTP_TIMEOUT: Final = httpx.Timeout(5.0)

EXPIRED: Final = "expired"
NOT_YET_VALID: Final = "not_yet_valid"
BAD_ISSUER: Final = "bad_issuer"
BAD_AUDIENCE: Final = "bad_audience"
BAD_SIGNATURE: Final = "bad_signature"
UNKNOWN_KID: Final = "unknown_kid"
UNSUPPORTED_ALG: Final = "unsupported_alg"
MISSING_CLAIM: Final = "missing_claim"
MALFORMED: Final = "malformed"
JWKS_UNAVAILABLE: Final = "jwks_unavailable"
REASONS: Final = frozenset(
    {
        EXPIRED,
        NOT_YET_VALID,
        BAD_ISSUER,
        BAD_AUDIENCE,
        BAD_SIGNATURE,
        UNKNOWN_KID,
        UNSUPPORTED_ALG,
        MISSING_CLAIM,
        MALFORMED,
        JWKS_UNAVAILABLE,
    }
)

_REQUIRED_CLAIMS: Final = ["iss", "sub", "aud", "exp", "iat"]


class _JwksFetchError(Exception):
    """JWKS could not be fetched or parsed (details are logged, never the response body)."""


class OidcTokenVerifier:
    """Verify Dex/OIDC RS256 JWTs. Thread-safe; share one instance per process."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuer = settings.OIDC_ISSUER_URL
        self._audience = settings.OIDC_AUDIENCE
        self._leeway = settings.OIDC_LEEWAY_SECONDS
        self._cache_seconds = float(settings.OIDC_JWKS_CACHE_SECONDS)
        self._jwks_url = settings.OIDC_INTERNAL_URL.rstrip("/") + "/keys"
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False)
        self._monotonic = monotonic

        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None  # monotonic time of the last fetch attempt
        self._loaded = False  # at least one successful fetch
        self._last_forced_at: float | None = None
        self._missing_kids: set[str] = set()  # already refetched for, still absent

    # -- public ------------------------------------------------------------------------
    def verify(self, token: str) -> VerifiedClaims:
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise InvalidTokenError(MALFORMED)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidTokenError(MALFORMED) from None

        if header.get("alg") != ALGORITHM:
            raise InvalidTokenError(UNSUPPORTED_ALG)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError(MALFORMED)

        key = self._key_for(kid)
        payload = self._decode(token, key)
        return self._claims(payload)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- decoding ------------------------------------------------------------------------
    def _decode(self, token: str, key: jwt.PyJWK) -> dict[str, Any]:
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_sub": True,
                    "require": _REQUIRED_CLAIMS,
                    "strict_aud": False,
                },
            )
        except jwt.InvalidSignatureError:
            raise InvalidTokenError(BAD_SIGNATURE) from None
        except jwt.ExpiredSignatureError:
            raise InvalidTokenError(EXPIRED) from None
        except jwt.ImmatureSignatureError:  # nbf or iat in the future beyond leeway
            raise InvalidTokenError(NOT_YET_VALID) from None
        except jwt.InvalidIssuerError:
            raise InvalidTokenError(BAD_ISSUER) from None
        except jwt.InvalidAudienceError:
            raise InvalidTokenError(BAD_AUDIENCE) from None
        except jwt.MissingRequiredClaimError:
            raise InvalidTokenError(MISSING_CLAIM) from None
        except jwt.InvalidAlgorithmError:
            raise InvalidTokenError(UNSUPPORTED_ALG) from None
        except jwt.PyJWTError:  # DecodeError, InvalidIssuedAtError, InvalidSubjectError, ...
            raise InvalidTokenError(MALFORMED) from None
        return payload

    def _claims(self, payload: Mapping[str, Any]) -> VerifiedClaims:
        # Belt and braces over the library checks: exact issuer, audience membership, sub.
        if payload.get("iss") != self._issuer:
            raise InvalidTokenError(BAD_ISSUER)
        aud = payload.get("aud")
        audience = (aud,) if isinstance(aud, str) else tuple(aud) if isinstance(aud, list) else ()
        if not all(isinstance(a, str) for a in audience):
            raise InvalidTokenError(MALFORMED)
        if self._audience not in audience:
            raise InvalidTokenError(BAD_AUDIENCE)
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise InvalidTokenError(MISSING_CLAIM)
        exp = payload.get("exp")
        if not isinstance(exp, int | float) or isinstance(exp, bool):
            raise InvalidTokenError(MALFORMED)

        email = payload.get("email")
        name = payload.get("name")
        return VerifiedClaims(
            issuer=self._issuer,
            subject=sub,
            email=email if isinstance(email, str) and email else None,
            email_verified=payload.get("email_verified") is True,
            name=name if isinstance(name, str) and name else None,
            audience=tuple(audience),
            expires_at=datetime.fromtimestamp(exp, UTC),
            raw=dict(payload),
        )

    # -- JWKS ------------------------------------------------------------------------------
    def _key_for(self, kid: str) -> jwt.PyJWK:
        with self._lock:
            now = self._monotonic()
            fetched_now = False
            if self._is_stale(now):
                self._refresh(now, forced=False)
                fetched_now = True

            key = self._keys.get(kid)
            if key is not None:
                return key

            if not fetched_now and self._may_force_refresh(kid, now):
                self._last_forced_at = now
                self._refresh(now, forced=True)
                key = self._keys.get(kid)
                if key is not None:
                    return key
                self._remember_missing(kid)
            elif fetched_now:
                self._remember_missing(kid)

            if not self._loaded:
                raise InvalidTokenError(JWKS_UNAVAILABLE)
            raise InvalidTokenError(UNKNOWN_KID)

    def _is_stale(self, now: float) -> bool:
        if self._fetched_at is None:
            return True
        if not self._loaded:
            return now - self._fetched_at >= FETCH_RETRY_SECONDS
        return now - self._fetched_at >= self._cache_seconds

    def _may_force_refresh(self, kid: str, now: float) -> bool:
        if kid in self._missing_kids:
            return False
        last = self._last_forced_at
        return last is None or now - last >= MIN_FORCED_REFRESH_SECONDS

    def _remember_missing(self, kid: str) -> None:
        if len(self._missing_kids) >= _MAX_REMEMBERED_MISSING_KIDS:
            self._missing_kids.clear()
        self._missing_kids.add(kid)

    def _refresh(self, now: float, *, forced: bool) -> None:
        try:
            keys = self._fetch_jwks()
        except _JwksFetchError as exc:
            logger.warning("jwks fetch failed", extra={"reason": str(exc), "forced": forced})
            if self._loaded:
                # Keep serving the cached keys; retry after a short delay, not on every request.
                self._fetched_at = now - self._cache_seconds + FETCH_RETRY_SECONDS
            else:
                self._fetched_at = now
            return
        self._keys = keys
        self._fetched_at = now
        self._loaded = True
        if not forced:
            self._missing_kids.clear()

    def _fetch_jwks(self) -> dict[str, jwt.PyJWK]:
        try:
            response = self._client.get(self._jwks_url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise _JwksFetchError(type(exc).__name__) from None
        if response.status_code != httpx.codes.OK:
            raise _JwksFetchError(f"http_{response.status_code}")
        try:
            document = response.json()
        except ValueError:
            raise _JwksFetchError("invalid_json") from None
        entries = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            raise _JwksFetchError("invalid_jwks")

        keys: dict[str, jwt.PyJWK] = {}
        for entry in entries:
            key = _rs256_signing_key(entry)
            if key is not None:
                keys[key.key_id or ""] = key
        keys.pop("", None)
        return keys


def _rs256_signing_key(entry: object) -> jwt.PyJWK | None:
    """An RS256 signing key from a JWK, or None for anything else (skipped, not trusted)."""
    if not isinstance(entry, dict):
        return None
    if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
        return None
    if entry.get("alg", ALGORITHM) != ALGORITHM:
        return None
    kid = entry.get("kid")
    if not isinstance(kid, str) or not kid:
        return None
    try:
        key = jwt.PyJWK(entry, algorithm=ALGORITHM)
    except jwt.PyJWTError:
        return None
    return key
