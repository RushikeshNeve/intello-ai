"""Document resource grants (M01.03.03).

Contract: ``docs/architecture/m01-contracts.md`` sections 2 (``resource_grants``) and 3. A grant
gives one grantee (a user, or every holder of one workspace role) a :class:`GrantPermission` on one
resource. A grant is **active** while ``revoked_at IS NULL AND (expires_at IS NULL OR expires_at >
now())``; activity is evaluated when it is read, so an expired grant stops authorizing without any
update. Expired or revoked grants never authorize.

All functions take a session opened with :func:`app.core.db.tenant_session` (app role, RLS on).
Every query is also filtered explicitly by org: by ``resource.org_id`` and by the transaction's
``app.current_org_id``, so a grant in org B is never read, created or revoked from an org A context,
even on a connection that bypasses RLS.

Audit is the caller's job, in the same transaction (contract section 5):

* after :func:`grant` returns a record with ``created=True``, record ``grant.created`` with
  ``target_type="resource_grant"``, ``target_id=str(record.id)`` and
  ``metadata=record.audit_metadata()``. A ``created=False`` result is an idempotent repeat and
  changed nothing.
* after :func:`revoke` returns ``True``, record ``grant.revoked`` (use :func:`get_grant` for the
  metadata). ``False`` means it was already revoked and nothing changed.

:func:`active_permissions` returns the set to put in ``SubjectContext.grants`` for
:func:`app.modules.authorization.policy.decide`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, cast

from sqlalchemy import (
    ColumnElement,
    Row,
    Table,
    and_,
    bindparam,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Session

from app.core.db import CURRENT_ORG_SETTING
from app.core.models import GRANT_RESOURCE_TYPES, ResourceGrant
from app.modules.authorization.policy import GrantPermission, WorkspaceRole

DOCUMENT: Final = "document"

# manage ⊃ write ⊃ read
_IMPLIES: Final[Mapping[GrantPermission, frozenset[GrantPermission]]] = {
    GrantPermission.READ: frozenset({GrantPermission.READ}),
    GrantPermission.WRITE: frozenset({GrantPermission.WRITE, GrantPermission.READ}),
    GrantPermission.MANAGE: frozenset(GrantPermission),
}

_T: Final = cast(Table, ResourceGrant.__table__)


class GranteeType(StrEnum):
    USER = "user"
    WORKSPACE_ROLE = "workspace_role"


class GrantError(Exception):
    """Base class for grant errors."""


class InvalidGrantError(GrantError, ValueError):
    """The request is malformed (naive or past ``expires_at``, unknown permission, ...)."""


class GrantScopeError(GrantError):
    """The resource's org is not the transaction's ``app.current_org_id`` (or none is set)."""


class GrantNotFoundError(GrantError, LookupError):
    """No grant with that id is visible in the current org."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceRef:
    """The resource a grant applies to. Only ``document`` exists in M01."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    resource_type: str = DOCUMENT
    resource_id: uuid.UUID

    def __post_init__(self) -> None:
        if self.resource_type not in GRANT_RESOURCE_TYPES:
            raise InvalidGrantError(f"unsupported resource_type: {self.resource_type!r}")


@dataclass(frozen=True, slots=True)
class Grantee:
    """A user (``id`` is the user uuid as text) or a workspace role (``id`` is the role name).

    Build with :meth:`user` or :meth:`role`.
    """

    type: GranteeType
    id: str

    def __post_init__(self) -> None:
        try:
            grantee_type = GranteeType(self.type)
        except ValueError as exc:
            raise InvalidGrantError(f"unknown grantee type: {self.type!r}") from exc
        object.__setattr__(self, "type", grantee_type)
        if grantee_type is GranteeType.USER:
            try:
                canonical = str(uuid.UUID(self.id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise InvalidGrantError("user grantee id must be a uuid") from exc
        else:
            try:
                canonical = WorkspaceRole(self.id).value
            except ValueError as exc:
                raise InvalidGrantError(f"unknown workspace role: {self.id!r}") from exc
        object.__setattr__(self, "id", canonical)

    @classmethod
    def user(cls, user_id: uuid.UUID) -> Grantee:
        return cls(GranteeType.USER, str(user_id))

    @classmethod
    def role(cls, role: WorkspaceRole | str) -> Grantee:
        return cls(GranteeType.WORKSPACE_ROLE, str(role))


@dataclass(frozen=True, slots=True)
class GrantRecord:
    """One ``resource_grants`` row. ``created`` is True only when :func:`grant` inserted it."""

    id: uuid.UUID
    resource: ResourceRef
    grantee: Grantee
    permission: GrantPermission
    granted_by: uuid.UUID
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by: uuid.UUID | None
    created: bool = field(default=False, compare=False)

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)

    def audit_metadata(self) -> dict[str, Any]:
        """Metadata for the caller's ``grant.created`` / ``grant.revoked`` audit event."""
        return {
            "resource_type": self.resource.resource_type,
            "resource_id": str(self.resource.resource_id),
            "workspace_id": str(self.resource.workspace_id),
            "grantee_type": self.grantee.type.value,
            "grantee_id": self.grantee.id,
            "permission": self.permission.value,
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
        }


def implied(perms: Iterable[GrantPermission | str]) -> frozenset[GrantPermission]:
    """Close ``perms`` under implication: ``manage ⊃ write ⊃ read``. Unknown values raise."""
    out: set[GrantPermission] = set()
    for perm in perms:
        out |= _IMPLIES[_permission(perm)]
    return frozenset(out)


def validate_expiry(expires_at: datetime | None, *, now: datetime) -> None:
    """``expires_at`` must be timezone-aware and strictly after ``now`` (or None: never expires)."""
    if expires_at is None:
        return
    _require_aware(expires_at, "expires_at")
    _require_aware(now, "now")
    if expires_at <= now:
        raise InvalidGrantError("expires_at must be in the future")


def grant(
    session: Session,
    *,
    resource: ResourceRef,
    grantee: Grantee,
    permission: GrantPermission | str,
    granted_by: uuid.UUID,
    expires_at: datetime | None = None,
) -> GrantRecord:
    """Grant ``permission`` on ``resource`` to ``grantee``; return the row.

    Idempotent: if an active grant with the same (resource, grantee, permission) already exists it
    is returned unchanged (``created=False``), whatever its ``expires_at``; nothing is written.
    Concurrent calls for the same key are serialized with a transaction-scoped advisory lock, so
    they produce one row. Grants of different permissions to the same grantee are separate rows.

    Raises :class:`InvalidGrantError` before any I/O for a naive or non-future ``expires_at`` and
    :class:`GrantScopeError` when ``resource.org_id`` is not the transaction's current org. The
    caller authorizes the actor (``document.manage``) and writes the ``grant.created`` audit event.
    """
    perm = _permission(permission)
    validate_expiry(expires_at, now=datetime.now(UTC))
    _require_org_context(session, resource.org_id)

    lock_key = "|".join(
        (
            "resource_grant",
            str(resource.org_id),
            str(resource.workspace_id),
            resource.resource_type,
            str(resource.resource_id),
            grantee.type.value,
            grantee.id,
            perm.value,
        )
    )
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))

    existing = session.execute(
        select(_T)
        .where(
            _resource_filter(resource),
            _T.c.grantee_type == grantee.type.value,
            _T.c.grantee_id == grantee.id,
            _T.c.permission == perm.value,
            _active(None),
        )
        .order_by(_T.c.created_at, _T.c.id)
        .limit(1)
    ).first()
    if existing is not None:
        return _record(existing, created=False)

    row = session.execute(
        insert(_T)
        .values(
            id=uuid.uuid4(),
            org_id=resource.org_id,
            workspace_id=resource.workspace_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            grantee_type=grantee.type.value,
            grantee_id=grantee.id,
            permission=perm.value,
            granted_by=granted_by,
            expires_at=expires_at,
        )
        .returning(*_T.c)
    ).one()
    return _record(row, created=True)


def revoke(session: Session, *, grant_id: uuid.UUID, revoked_by: uuid.UUID) -> bool:
    """Revoke a grant. True if this call revoked it; False if it was already revoked.

    A second revoke changes nothing: the original ``revoked_at`` / ``revoked_by`` are kept. An
    expired but unrevoked grant is revoked normally (True). Raises :class:`GrantNotFoundError`
    when no grant with that id is visible in the current org (including another org's grant).
    Rows are never deleted. The caller writes the ``grant.revoked`` audit event.
    """
    revoked = session.execute(
        update(_T)
        .where(_T.c.id == grant_id, _in_current_org(), _T.c.revoked_at.is_(None))
        .values(revoked_at=func.now(), revoked_by=revoked_by)
        .returning(_T.c.id)
    ).first()
    if revoked is not None:
        return True
    exists = session.execute(select(_T.c.id).where(_T.c.id == grant_id, _in_current_org())).first()
    if exists is None:
        raise GrantNotFoundError(str(grant_id))
    return False


def get_grant(session: Session, *, grant_id: uuid.UUID) -> GrantRecord | None:
    """The grant with that id in the current org, active or not (None if not visible)."""
    row = session.execute(select(_T).where(_T.c.id == grant_id, _in_current_org())).first()
    return None if row is None else _record(row)


def list_grants(
    session: Session, *, resource: ResourceRef, include_inactive: bool = False
) -> list[GrantRecord]:
    """Grants on ``resource`` (active only by default), oldest first."""
    stmt = select(_T).where(_resource_filter(resource))
    if not include_inactive:
        stmt = stmt.where(_active(None))
    rows = session.execute(stmt.order_by(_T.c.created_at, _T.c.id)).all()
    return [_record(row) for row in rows]


def active_permissions(
    session: Session,
    *,
    resource: ResourceRef,
    user_id: uuid.UUID,
    workspace_role: WorkspaceRole | str | None,
    now: datetime | None = None,
) -> frozenset[GrantPermission]:
    """Permissions ``user_id`` holds on ``resource`` via active grants, closed under implication.

    Matches grants to the user and grants to ``workspace_role`` (exactly that role; pass the user's
    explicit workspace role, or None). Revocation and expiry are evaluated now: at the database's
    ``now()`` (transaction start) or at the injected timezone-aware ``now``. Returns an empty set
    when ``resource.org_id`` is not the current org (fail closed). Put the result in
    ``SubjectContext.grants``.
    """
    if now is not None:
        _require_aware(now, "now")
    grantee_match: ColumnElement[bool] = and_(
        _T.c.grantee_type == GranteeType.USER.value, _T.c.grantee_id == str(user_id)
    )
    if workspace_role is not None:
        role = Grantee.role(workspace_role).id
        grantee_match = or_(
            grantee_match,
            and_(_T.c.grantee_type == GranteeType.WORKSPACE_ROLE.value, _T.c.grantee_id == role),
        )
    rows = session.execute(
        select(_T.c.permission)
        .distinct()
        .where(_resource_filter(resource), grantee_match, _active(now))
    ).scalars()
    return implied(rows)


# --- internals ------------------------------------------------------------------------------


def _permission(value: GrantPermission | str) -> GrantPermission:
    try:
        return GrantPermission(value)
    except ValueError as exc:
        raise InvalidGrantError(f"unknown grant permission: {value!r}") from exc


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidGrantError(f"{name} must be a timezone-aware datetime")


def _current_org() -> ColumnElement[Any]:
    return func.nullif(func.current_setting(CURRENT_ORG_SETTING, True), "").cast(UUID(as_uuid=True))


def _in_current_org() -> ColumnElement[bool]:
    return _T.c.org_id == _current_org()


def _require_org_context(session: Session, org_id: uuid.UUID) -> None:
    current = session.execute(select(_current_org())).scalar_one_or_none()
    if current != org_id:
        raise GrantScopeError("resource org is not the current org context")


def _resource_filter(resource: ResourceRef) -> ColumnElement[bool]:
    return and_(
        _T.c.org_id == resource.org_id,
        _in_current_org(),
        _T.c.workspace_id == resource.workspace_id,
        _T.c.resource_type == resource.resource_type,
        _T.c.resource_id == resource.resource_id,
    )


def _active(now: datetime | None) -> ColumnElement[bool]:
    at: ColumnElement[Any] = (
        func.now() if now is None else bindparam("eval_now", now, type_=TIMESTAMP(timezone=True))
    )
    return and_(_T.c.revoked_at.is_(None), or_(_T.c.expires_at.is_(None), _T.c.expires_at > at))


def _record(row: Row[Any], *, created: bool = False) -> GrantRecord:
    m = row._mapping
    return GrantRecord(
        id=m["id"],
        resource=ResourceRef(
            org_id=m["org_id"],
            workspace_id=m["workspace_id"],
            resource_type=m["resource_type"],
            resource_id=m["resource_id"],
        ),
        grantee=Grantee(GranteeType(m["grantee_type"]), m["grantee_id"]),
        permission=GrantPermission(m["permission"]),
        granted_by=m["granted_by"],
        created_at=m["created_at"],
        expires_at=m["expires_at"],
        revoked_at=m["revoked_at"],
        revoked_by=m["revoked_by"],
        created=created,
    )
