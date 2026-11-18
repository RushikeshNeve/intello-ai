"""Shared request helpers for tenant-scoped routes (contract docs/architecture/m01-contracts.md).

* :func:`get_request_id`: a safe client ``X-Request-ID`` or a fresh UUID, for audit rows.
* :class:`Pagination` / :class:`KeysetCursor`: ``?limit=`` (1..200, default 50) and an opaque
  ``cursor`` for keyset pagination on ``(created_at, id)``.
* :class:`TenantRequest`: the per-request bundle (principal, tenant session factory, request id).
  :meth:`TenantRequest.transaction` opens one tenant transaction and turns what goes wrong in it
  into the contract's HTTP errors:

  - :class:`AuthzDenied` raised inside it rolls the business transaction back, then writes the
    ``authz.denied`` audit event in its **own** transaction (so the rollback can't lose it) and
    answers 404 ``not_found`` when the policy says the denial must be concealed, else 403
    ``forbidden``;
  - ``IntegrityError`` from a statement or from COMMIT (the deferred owner trigger fires there) is
    mapped by :func:`map_integrity_error` to 409 ``last_owner`` / ``slug_taken``.

Errors are always ``{"detail": "<stable_code>"}``.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import TenantSessionFactory, get_tenant_session_factory
from app.core.models import ORG_OWNER_CONSTRAINT, ORG_SLUG_UNIQUE, WORKSPACE_SLUG_UNIQUE
from app.core.security import Principal, get_current_principal
from app.modules.audit import record_denied
from app.modules.authorization.policy import Decision, is_concealed

# Stable error codes used by the tenancy routes.
NOT_FOUND: Final = "not_found"
FORBIDDEN: Final = "forbidden"
LAST_OWNER: Final = "last_owner"
SLUG_TAKEN: Final = "slug_taken"
NOT_ORG_MEMBER: Final = "not_org_member"
USER_NOT_FOUND: Final = "user_not_found"
INVALID_CURSOR: Final = "invalid_cursor"
INVALID_REQUEST: Final = "invalid_request"
CONFLICT: Final = "conflict"

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200

_SLUG_FORMAT_CONSTRAINTS: Final = frozenset(
    {"ck_organizations_slug_format", "ck_workspaces_slug_format"}
)
_REQUEST_ID_HEADER: Final = "x-request-id"
_SAFE_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def api_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=code)


def not_found() -> HTTPException:
    return api_error(status.HTTP_404_NOT_FOUND, NOT_FOUND)


# --- request id ---------------------------------------------------------------------------


def get_request_id(request: Request) -> str:
    """The client's ``X-Request-ID`` when it is short and safe, else a new UUID (cached)."""
    cached = getattr(request.state, "request_id", None)
    if isinstance(cached, str):
        return cached
    supplied = request.headers.get(_REQUEST_ID_HEADER, "")
    request_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
    request.state.request_id = request_id
    return request_id


# --- pagination ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeysetCursor:
    """Opaque keyset position ``(created_at, id)`` (url-safe base64, no padding)."""

    created_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> KeysetCursor:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            stamp, ident = raw.split("|", 1)
            created_at = datetime.fromisoformat(stamp)
            if created_at.tzinfo is None:
                raise ValueError("naive timestamp")
            return cls(created_at=created_at, id=uuid.UUID(ident))
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ValueError("invalid cursor") from exc


@dataclass(frozen=True)
class Pagination:
    limit: int
    after: KeysetCursor | None


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Pagination:
    after: KeysetCursor | None = None
    if cursor:
        try:
            after = KeysetCursor.decode(cursor)
        except ValueError:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None
    return Pagination(limit=limit, after=after)


# --- errors -------------------------------------------------------------------------------


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def map_integrity_error(exc: IntegrityError) -> HTTPException | None:
    """Contract §2 DB error -> HTTP mapping; ``None`` when the error is not a known case.

    ``ck_*_slug_format`` only reaches the database for a slug that is valid apart from upper-case
    letters (request validation rejects every other shape): the stored slugs are lower-case, so
    that is a case-variant collision -> ``slug_taken``.
    """
    name = _constraint_name(exc)
    if name == ORG_OWNER_CONSTRAINT:
        return api_error(status.HTTP_409_CONFLICT, LAST_OWNER)
    if name in (ORG_SLUG_UNIQUE, WORKSPACE_SLUG_UNIQUE) or name in _SLUG_FORMAT_CONSTRAINTS:
        return api_error(status.HTTP_409_CONFLICT, SLUG_TAKEN)
    return None


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """422 ``{"detail": "invalid_request", "errors": [{"loc", "msg", "type"}]}``.

    Input values are not echoed back (they may carry anything the client sent).
    """
    errors: list[dict[str, Any]] = []
    if isinstance(exc, RequestValidationError):
        errors = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=jsonable_encoder({"detail": INVALID_REQUEST, "errors": errors}),
    )


# --- tenant request -----------------------------------------------------------------------


class AuthzDenied(Exception):
    """Raise inside :meth:`TenantRequest.transaction` to deny (and audit) the request."""

    def __init__(
        self,
        decision: Decision,
        *,
        action: str,
        org_id: uuid.UUID,
        target_type: str,
        target_id: str,
    ) -> None:
        super().__init__(decision.reason)
        self.decision = decision
        self.action = action
        self.org_id = org_id
        self.target_type = target_type
        self.target_id = target_id


@dataclass(frozen=True)
class TenantRequest:
    principal: Principal
    sessions: TenantSessionFactory
    request_id: str

    @property
    def user_id(self) -> uuid.UUID:
        return self.principal.user_id

    @contextmanager
    def transaction(self, org_id: uuid.UUID | None) -> Iterator[Session]:
        """One tenant transaction (user = the principal, org = ``org_id`` from the path)."""
        try:
            with self.sessions(user_id=self.user_id, org_id=org_id) as session:
                yield session
        except AuthzDenied as denied:
            # The business transaction is rolled back; the denial is kept in its own one.
            record_denied(
                self.sessions,
                actor_user_id=self.user_id,
                org_id=denied.org_id,
                target_type=denied.target_type,
                target_id=denied.target_id,
                request_id=self.request_id,
                metadata={"action": denied.action, "reason": denied.decision.reason},
            )
            if is_concealed(denied.decision):
                raise not_found() from None
            raise api_error(status.HTTP_403_FORBIDDEN, FORBIDDEN) from None
        except IntegrityError as exc:
            mapped = map_integrity_error(exc)
            if mapped is None:
                raise
            raise mapped from None


def get_tenant_request(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    sessions: Annotated[TenantSessionFactory, Depends(get_tenant_session_factory)],
) -> TenantRequest:
    return TenantRequest(principal=principal, sessions=sessions, request_id=get_request_id(request))


Tenant = Annotated[TenantRequest, Depends(get_tenant_request)]
Page = Annotated[Pagination, Depends(get_pagination)]
