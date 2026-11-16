"""Unit tests for document grants (M01.03.03): implication, value types and validation.

DB-backed behaviour (grant/revoke/expiry/RLS) is in ``tests/integration/m01_grants``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app.modules.authorization.grants import (
    Grantee,
    GranteeType,
    GrantRecord,
    InvalidGrantError,
    ResourceRef,
    active_permissions,
    grant,
    implied,
    validate_expiry,
)
from app.modules.authorization.policy import GrantPermission, WorkspaceRole

R, W, M = GrantPermission.READ, GrantPermission.WRITE, GrantPermission.MANAGE
USER = uuid.UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _NoIO:
    """A session stand-in that fails the test on any use: validation must happen before I/O."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"session.{name} used before validation")


NO_IO = cast(Session, _NoIO())


def _resource() -> ResourceRef:
    return ResourceRef(org_id=uuid.uuid4(), workspace_id=uuid.uuid4(), resource_id=uuid.uuid4())


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ([], frozenset()),
        ([R], {R}),
        ([W], {W, R}),
        ([M], {M, W, R}),
        ([R, W], {W, R}),
        ([R, M], {M, W, R}),
        (["read"], {R}),
        (["manage", "read"], {M, W, R}),
    ],
)
def test_implied(given: list[GrantPermission | str], expected: set[GrantPermission]) -> None:
    assert implied(given) == frozenset(expected)


def test_implied_is_idempotent_and_accepts_any_iterable() -> None:
    once = implied(iter([W]))
    assert implied(once) == once
    assert isinstance(once, frozenset)


def test_implied_rejects_unknown_permission() -> None:
    with pytest.raises(InvalidGrantError):
        implied(["admin"])


def test_resource_ref_defaults_to_document_and_rejects_other_types() -> None:
    assert _resource().resource_type == "document"
    with pytest.raises(InvalidGrantError):
        ResourceRef(
            org_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            resource_type="folder",
            resource_id=uuid.uuid4(),
        )


def test_resource_ref_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        ResourceRef(uuid.uuid4(), uuid.uuid4(), "document", uuid.uuid4())  # type: ignore[call-arg]


def test_grantee_user_and_role() -> None:
    user = Grantee.user(USER)
    assert (user.type, user.id) == (GranteeType.USER, str(USER))
    role = Grantee.role(WorkspaceRole.EDITOR)
    assert (role.type, role.id) == (GranteeType.WORKSPACE_ROLE, "editor")
    assert Grantee.role("viewer") == Grantee(GranteeType.WORKSPACE_ROLE, "viewer")


def test_grantee_user_id_is_canonicalised() -> None:
    upper = Grantee(GranteeType.USER, str(USER).upper())
    assert upper.id == str(USER)
    assert upper == Grantee.user(USER)


@pytest.mark.parametrize(
    ("grantee_type", "grantee_id"),
    [
        ("user", "not-a-uuid"),
        ("user", "editor"),
        ("workspace_role", "owner"),
        ("workspace_role", str(USER)),
    ],
)
def test_grantee_rejects_mismatched_id(grantee_type: str, grantee_id: str) -> None:
    with pytest.raises(InvalidGrantError):
        Grantee(GranteeType(grantee_type), grantee_id)


def test_grantee_rejects_unknown_type() -> None:
    with pytest.raises(InvalidGrantError, match="group"):
        Grantee("group", "x")  # type: ignore[arg-type]


def test_validate_expiry_accepts_none_and_future() -> None:
    validate_expiry(None, now=NOW)
    validate_expiry(NOW + timedelta(seconds=1), now=NOW)
    # Any timezone is fine as long as it is aware.
    validate_expiry(datetime(2026, 1, 1, 14, 0, 1, tzinfo=timezone(timedelta(hours=2))), now=NOW)


@pytest.mark.parametrize(
    "expires_at",
    [
        NOW,
        NOW - timedelta(seconds=1),
        datetime(2026, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_validate_expiry_rejects_now_and_past(expires_at: datetime) -> None:
    with pytest.raises(InvalidGrantError, match="future"):
        validate_expiry(expires_at, now=NOW)


def test_validate_expiry_rejects_naive() -> None:
    with pytest.raises(InvalidGrantError, match="timezone-aware"):
        validate_expiry(datetime(2999, 1, 1), now=NOW)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"expires_at": datetime.now(UTC) - timedelta(minutes=1)}, "future"),
        ({"expires_at": datetime(2999, 1, 1)}, "timezone-aware"),
        ({"permission": "own"}, "permission"),
    ],
)
def test_grant_validates_before_any_io(kwargs: dict[str, Any], match: str) -> None:
    args: dict[str, Any] = {
        "resource": _resource(),
        "grantee": Grantee.user(USER),
        "permission": R,
        "granted_by": USER,
    }
    args.update(kwargs)
    with pytest.raises(InvalidGrantError, match=match):
        grant(NO_IO, **args)


def test_active_permissions_rejects_naive_now_before_any_io() -> None:
    with pytest.raises(InvalidGrantError, match="timezone-aware"):
        active_permissions(
            NO_IO,
            resource=_resource(),
            user_id=USER,
            workspace_role=None,
            now=datetime(2026, 1, 1),
        )


def test_active_permissions_rejects_unknown_role_before_any_io() -> None:
    with pytest.raises(InvalidGrantError):
        active_permissions(NO_IO, resource=_resource(), user_id=USER, workspace_role="owner")


def _record(**overrides: Any) -> GrantRecord:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "resource": _resource(),
        "grantee": Grantee.role("viewer"),
        "permission": W,
        "granted_by": USER,
        "created_at": NOW,
        "expires_at": None,
        "revoked_at": None,
        "revoked_by": None,
    }
    values.update(overrides)
    return GrantRecord(**values)


def test_record_is_active() -> None:
    assert _record().is_active(NOW)
    assert _record(expires_at=NOW + timedelta(seconds=1)).is_active(NOW)
    assert not _record(expires_at=NOW).is_active(NOW)
    assert not _record(revoked_at=NOW, revoked_by=USER).is_active(NOW)


def test_record_audit_metadata_has_no_secrets_and_is_json_ready() -> None:
    record = _record(expires_at=NOW + timedelta(days=1))
    meta = record.audit_metadata()
    assert meta == {
        "resource_type": "document",
        "resource_id": str(record.resource.resource_id),
        "workspace_id": str(record.resource.workspace_id),
        "grantee_type": "workspace_role",
        "grantee_id": "viewer",
        "permission": "write",
        "expires_at": "2026-01-02T12:00:00+00:00",
    }
