"""Forge test JWTs with local RSA keys and serve them from a fake JWKS (httpx MockTransport)."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import Settings

ISSUER = "http://127.0.0.1:28381/dex"
INTERNAL = "http://dex.internal:5556/dex"
AUDIENCE = "ukos-api"
SUBJECT = "CiQ2YjFjNGYwZS0xYTUyLTRjOGUtOWEwZC0wMDAwMDAwMDAwMDESBWxvY2Fs"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "OIDC_ISSUER_URL": ISSUER,
        "OIDC_INTERNAL_URL": INTERNAL,
        "OIDC_AUDIENCE": AUDIENCE,
        "OIDC_JWKS_CACHE_SECONDS": 300,
        "OIDC_LEEWAY_SECONDS": 30,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class RsaKey:
    kid: str
    private: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )

    def jwk(self, **extra: Any) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key())
        )
        data.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        data.update(extra)
        return data

    def public_pem(self) -> bytes:
        return self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def sign(self, claims: dict[str, Any], *, headers: dict[str, Any] | None = None) -> str:
        return jwt.encode(
            claims, self.private, algorithm="RS256", headers={"kid": self.kid, **(headers or {})}
        )

    def sign_raw(self, payload: dict[str, Any]) -> str:
        """RS256-sign any JSON payload (bypasses PyJWT's encode-side claim checks)."""
        header = {"alg": "RS256", "kid": self.kid, "typ": "JWT"}
        signing_input = (
            f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
        )
        sig = self.private.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{b64url(sig)}"


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    values: dict[str, Any] = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": [AUDIENCE, "ukos-web"],
        "azp": "ukos-web",
        "exp": now + 600,
        "iat": now,
        "email": "alice@ukos.test",
        "email_verified": True,
        "name": "alice",
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned_token(header: dict[str, Any], payload: dict[str, Any], signature: bytes = b"") -> str:
    return ".".join(
        [
            b64url(json.dumps(header).encode()),
            b64url(json.dumps(payload).encode()),
            b64url(signature),
        ]
    )


class FakeJwks:
    """Serves ``{"keys": self.keys}`` at ``INTERNAL/keys`` and counts requests."""

    def __init__(self, *keys: dict[str, Any]) -> None:
        self.keys: list[dict[str, Any]] = list(keys)
        self.calls = 0
        self.status = 200
        self.fail: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{INTERNAL}/keys"
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return httpx.Response(self.status, json={"keys": self.keys})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now
