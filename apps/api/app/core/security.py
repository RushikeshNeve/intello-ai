"""Authentication core (contract docs/architecture/m01-contracts.md §1).

``get_current_principal`` is the only way a route learns who is calling:
Bearer token -> ``TokenVerifier.verify`` -> ``UserProvisioner.provision`` -> ``Principal``.

The two pieces are replaceable FastAPI dependencies. The real implementations live in
``app/modules/identity/oidc.py`` (M01.01.01) and ``app/modules/identity/provisioning.py``
(M01.01.03) and are wired in ``create_app`` with::

    app.dependency_overrides[get_token_verifier] = lambda: OidcTokenVerifier(...)
    app.dependency_overrides[get_user_provisioner] = lambda: DbUserProvisioner(...)

Until then the defaults raise :class:`NotConfiguredError`, which ``create_app`` maps to a 500.

Failure contract: any authentication failure is **401** with
``WWW-Authenticate: Bearer error="invalid_token"`` and body ``{"detail": "invalid_token"}``.
Verifiers signal failure by raising :class:`InvalidTokenError`; its ``reason`` is logged, the token
never is.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Protocol, runtime_checkable

from fastapi import Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("ukos.security")

INVALID_TOKEN = "invalid_token"  # noqa: S105 - error code, not a secret
WWW_AUTHENTICATE_INVALID_TOKEN = 'Bearer error="invalid_token"'  # noqa: S105


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID  # users.id (internal)
    issuer: str
    subject: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class VerifiedClaims:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    name: str | None
    audience: tuple[str, ...]
    expires_at: datetime
    raw: Mapping[str, Any] = field(repr=False)


class InvalidTokenError(Exception):
    """Raised by a verifier for any token problem. ``reason`` is for logs only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotConfiguredError(RuntimeError):
    """A required security component has not been wired (maps to HTTP 500)."""


@runtime_checkable
class TokenVerifier(Protocol):
    def verify(self, token: str) -> VerifiedClaims: ...


@runtime_checkable
class UserProvisioner(Protocol):
    def provision(self, claims: VerifiedClaims) -> Principal: ...


def invalid_token_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=INVALID_TOKEN,
        headers={"WWW-Authenticate": WWW_AUTHENTICATE_INVALID_TOKEN},
    )


def get_token_verifier() -> TokenVerifier:
    """Default: not wired. Overridden in ``create_app`` by the identity module (M01.01.01)."""
    raise NotConfiguredError("token verifier is not configured")


def get_user_provisioner() -> UserProvisioner:
    """Default: not wired. Overridden in ``create_app`` by the identity module (M01.01.03)."""
    raise NotConfiguredError("user provisioner is not configured")


def extract_bearer_token(authorization: str | None) -> str:
    """Return the token from ``Authorization: Bearer <token>`` or raise a 401."""
    if not authorization:
        raise invalid_token_exception()
    scheme, sep, token = authorization.strip().partition(" ")
    token = token.strip()
    if not sep or scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
        raise invalid_token_exception()
    return token


async def get_current_principal(
    request: Request,
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    provisioner: Annotated[UserProvisioner, Depends(get_user_provisioner)],
) -> Principal:
    """FastAPI dependency: verify -> provision -> Principal.

    Verification and provisioning are sync (JWKS fetch, DB upsert), so they run in the
    threadpool to keep the event loop free.
    """
    token = extract_bearer_token(request.headers.get("authorization"))
    try:
        claims = await run_in_threadpool(verifier.verify, token)
    except InvalidTokenError as exc:
        logger.info("bearer token rejected", extra={"reason": exc.reason})
        raise invalid_token_exception() from None
    return await run_in_threadpool(provisioner.provision, claims)
