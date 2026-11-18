"""Unit tests for the shared tenancy helpers (no database)."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    AuthzDenied,
    KeysetCursor,
    TenantRequest,
    get_pagination,
    get_request_id,
    map_integrity_error,
)
from app.core.db import TenantSessionFactory
from app.core.security import (
    Principal,
    VerifiedClaims,
    get_token_verifier,
    get_user_provisioner,
)
from app.main import create_app
from app.modules.authorization.policy import Decision
from app.modules.organizations.schemas import MemberRoleIn, OrgCreate, OrgUpdate

USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG = uuid.UUID("33333333-3333-4333-8333-333333333333")


def test_cursor_round_trip_and_opacity() -> None:
    cursor = KeysetCursor(created_at=datetime(2026, 11, 18, 12, 0, tzinfo=UTC), id=USER)
    token = cursor.encode()
    assert "=" not in token
    assert "|" not in token
    assert KeysetCursor.decode(token) == cursor


@pytest.mark.parametrize(
    "token",
    ["", "!!!", "Zm9v", "MjAyNi0xMS0xOFQxMjowMDowMHwx", "MjAyNi0xMS0xOFQxMjowMDowMHw="],
)
def test_cursor_rejects_garbage(token: str) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        KeysetCursor.decode(token)


def test_naive_cursor_timestamp_is_rejected() -> None:
    token = base64.urlsafe_b64encode(f"2026-01-01T00:00:00|{USER}".encode()).decode()
    with pytest.raises(ValueError, match="invalid cursor"):
        KeysetCursor.decode(token)


def test_get_pagination_maps_a_bad_cursor_to_422() -> None:
    with pytest.raises(HTTPException) as info:
        get_pagination(limit=10, cursor="garbage")
    assert (info.value.status_code, info.value.detail) == (422, "invalid_cursor")
    page = get_pagination(limit=10, cursor=None)
    assert (page.limit, page.after) == (10, None)


class _Diag:
    def __init__(self, name: str | None) -> None:
        self.constraint_name = name


class _Orig(Exception):
    def __init__(self, name: str | None) -> None:
        super().__init__(name)
        self.diag = _Diag(name)


def _integrity(name: str | None) -> IntegrityError:
    return IntegrityError("stmt", {}, _Orig(name))


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("organization_has_owner", (409, "last_owner")),
        ("uq_organizations_slug", (409, "slug_taken")),
        ("uq_workspaces_org_id_slug", (409, "slug_taken")),
        ("ck_organizations_slug_format", (422, "invalid_request")),
        ("ck_workspaces_slug_format", (422, "invalid_request")),
    ],
)
def test_map_integrity_error(constraint: str, expected: tuple[int, str]) -> None:
    mapped = map_integrity_error(_integrity(constraint))
    assert mapped is not None
    assert (mapped.status_code, mapped.detail) == expected


@pytest.mark.parametrize(
    "constraint", [None, "fk_workspaces_org_id", "pk_organization_memberships"]
)
def test_unknown_integrity_errors_are_not_mapped(constraint: str | None) -> None:
    assert map_integrity_error(_integrity(constraint)) is None


def _request_id_app() -> TestClient:
    app = FastAPI()

    @app.get("/rid")
    def rid(request_id: Annotated[str, Depends(get_request_id)]) -> str:
        return request_id

    return TestClient(app)


def test_request_id_uses_safe_client_value_or_generates_one() -> None:
    client = _request_id_app()
    assert client.get("/rid", headers={"X-Request-ID": "abc-123.x:y_z"}).json() == "abc-123.x:y_z"
    for bad in ("has space", "x" * 129, "semi;colon", ""):
        value = client.get("/rid", headers={"X-Request-ID": bad}).json()
        uuid.UUID(value)
    uuid.UUID(client.get("/rid").json())


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, stmt: Any, *_: Any) -> None:
        self.statements.append(stmt)


class _FakeSessions:
    """Stands in for ``TenantSessionFactory``: records which contexts were opened."""

    def __init__(self) -> None:
        self.opened: list[tuple[uuid.UUID | None, uuid.UUID | None]] = []
        self.sessions: list[_RecordingSession] = []

    @contextmanager
    def __call__(self, *, user_id: uuid.UUID | None, org_id: uuid.UUID | None) -> Iterator[Session]:
        self.opened.append((user_id, org_id))
        session = _RecordingSession()
        self.sessions.append(session)
        yield cast(Session, session)


def _tenant(sessions: _FakeSessions) -> TenantRequest:
    principal = Principal(user_id=USER, issuer="i", subject="s", email=None, display_name=None)
    return TenantRequest(
        principal=principal, sessions=cast(TenantSessionFactory, sessions), request_id="req-1"
    )


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("not_org_member", (404, "not_found")),
        ("not_workspace_member", (404, "not_found")),
        ("insufficient_role", (403, "forbidden")),
    ],
)
def test_denial_is_audited_in_its_own_transaction(reason: str, expected: tuple[int, str]) -> None:
    sessions = _FakeSessions()
    tenant = _tenant(sessions)
    with pytest.raises(HTTPException) as info, tenant.transaction(ORG):
        raise AuthzDenied(
            Decision(allowed=False, reason=reason),
            action="org.update",
            org_id=ORG,
            target_type="organization",
            target_id=str(ORG),
        )
    assert (info.value.status_code, info.value.detail) == expected
    # One business transaction (rolled back by the real factory) + one for the denial.
    assert sessions.opened == [(USER, ORG), (USER, ORG)]
    assert sessions.sessions[0].statements == []
    (stmt,) = sessions.sessions[1].statements
    params = stmt.compile().params
    assert params["event_type"] == "authz.denied"
    assert params["outcome"] == "denied"
    assert params["request_id"] == "req-1"
    assert params["metadata"] == {"action": "org.update", "reason": reason}


def test_integrity_errors_from_commit_are_mapped() -> None:
    sessions = _FakeSessions()
    with pytest.raises(HTTPException) as info, _tenant(sessions).transaction(ORG):
        raise _integrity("organization_has_owner")
    assert (info.value.status_code, info.value.detail) == (409, "last_owner")
    with pytest.raises(IntegrityError), _tenant(sessions).transaction(ORG):
        raise _integrity("something_else")


@pytest.mark.parametrize("slug", ["ab", "a-b-c", "x" * 63, "0rg"])
def test_slug_input_accepts_valid_slugs(slug: str) -> None:
    assert OrgCreate(slug=slug, name="n").slug == slug


@pytest.mark.parametrize(
    "slug", ["a", "-ab", "a b", "x" * 64, "ü-org", "a_b", "", "ACME", "Acme-Co"]
)
def test_slug_input_rejects_bad_shapes(slug: str) -> None:
    with pytest.raises(ValidationError):
        OrgCreate(slug=slug, name="n")


def test_names_are_trimmed_and_bounded() -> None:
    assert OrgCreate(slug="ab", name="  x  ").name == "x"
    assert OrgUpdate().name is None
    for bad in ("", "   ", "x" * 201):
        with pytest.raises(ValidationError):
            OrgUpdate(name=bad)
    with pytest.raises(ValidationError):
        OrgUpdate.model_validate({"name": "x", "slug": "nope"})
    with pytest.raises(ValidationError):
        MemberRoleIn.model_validate({"role": "editor"})


class _Provisioner:
    def provision(self, claims: VerifiedClaims) -> Principal:
        return Principal(
            user_id=USER,
            issuer=claims.issuer,
            subject=claims.subject,
            email=None,
            display_name=None,
        )


class _Verifier:
    def verify(self, token: str) -> VerifiedClaims:
        return VerifiedClaims(
            issuer="i",
            subject=token,
            email=None,
            email_verified=False,
            name=None,
            audience=("ukos-api",),
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            raw={},
        )


def test_routes_are_registered_and_validation_errors_use_the_code_shape() -> None:
    app = create_app()
    paths = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}
    base = "/v1/orgs/{org_id}"
    ws = base + "/workspaces/{workspace_id}"
    assert {
        ("GET", "/v1/orgs"),
        ("POST", "/v1/orgs"),
        ("GET", base),
        ("PATCH", base),
        ("DELETE", base),
        ("GET", base + "/members"),
        ("PUT", base + "/members/{user_id}"),
        ("DELETE", base + "/members/{user_id}"),
        ("GET", base + "/workspaces"),
        ("POST", base + "/workspaces"),
        ("GET", ws),
        ("PATCH", ws),
        ("DELETE", ws),
        ("PUT", ws + "/members/{user_id}"),
        ("DELETE", ws + "/members/{user_id}"),
    } <= paths

    app.dependency_overrides[get_token_verifier] = _Verifier
    app.dependency_overrides[get_user_provisioner] = _Provisioner
    client = TestClient(app)
    auth = {"Authorization": "Bearer t"}
    for method, path in [
        ("GET", "/v1/orgs?limit=0"),
        ("GET", "/v1/orgs?limit=201"),
        ("GET", "/v1/orgs/not-a-uuid"),
        ("DELETE", f"/v1/orgs/{ORG}/members/42"),
    ]:
        res = client.request(method, path, headers=auth)
        assert res.status_code == 422, path
        body = res.json()
        assert body["detail"] == "invalid_request"
        assert body["errors"]
        assert set(body["errors"][0]) == {"loc", "msg", "type"}
    res = client.post("/v1/orgs", headers=auth, json={"slug": "secret-value!", "name": "x"})
    assert res.status_code == 422
    assert "secret-value!" not in res.text  # input is never echoed back
