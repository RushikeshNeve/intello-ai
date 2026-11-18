"""Helpers for the tenancy API tests: a fake token verifier, users, requests and audit reads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.security import InvalidTokenError, VerifiedClaims

ISSUER = "http://issuer.tenancy.test"


class FakeVerifier:
    """``TokenVerifier`` mapping opaque test tokens to claims (everything else is invalid)."""

    def __init__(self) -> None:
        self._claims: dict[str, VerifiedClaims] = {}

    def register(self, token: str, *, subject: str, email: str, name: str) -> None:
        self._claims[token] = VerifiedClaims(
            issuer=ISSUER,
            subject=subject,
            email=email,
            email_verified=True,
            name=name,
            audience=("ukos-api",),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            raw={"iss": ISSUER, "sub": subject},
        )

    def verify(self, token: str) -> VerifiedClaims:
        claims = self._claims.get(token)
        if claims is None:
            raise InvalidTokenError("unknown_test_token")
        return claims


@dataclass(frozen=True)
class User:
    id: uuid.UUID
    email: str
    name: str
    token: str


@dataclass
class Response:
    status: int
    body: Any
    request_id: str
    raw: httpx.Response = field(repr=False)


@dataclass(frozen=True)
class Tenant:
    """An org with one user per role, plus an outsider (owner of another org)."""

    org_id: uuid.UUID
    slug: str
    owner: User
    admin: User
    member: User
    outsider: User
    other_org_id: uuid.UUID


class Api:
    def __init__(self, client: TestClient, verifier: FakeVerifier, admin_engine: Engine) -> None:
        self.client = client
        self.verifier = verifier
        self.admin_engine = admin_engine

    # -- users and requests ------------------------------------------------------------
    def user(self, label: str = "user") -> User:
        """A fresh user, provisioned through the real provisioner by one ``GET /v1/me``."""
        suffix = uuid.uuid4().hex[:10]
        token = f"tok-{label}-{suffix}"
        email = f"{label}-{suffix}@tenancy.test"
        name = f"{label.title()} {suffix}"
        self.verifier.register(token, subject=f"sub-{label}-{suffix}", email=email, name=name)
        res = self.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.text
        return User(id=uuid.UUID(res.json()["id"]), email=email, name=name, token=token)

    def call(
        self,
        method: str,
        path: str,
        user: User | None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        request_id = f"req-{uuid.uuid4().hex}"
        headers = {"X-Request-ID": request_id}
        if user is not None:
            headers["Authorization"] = f"Bearer {user.token}"
        res = self.client.request(method, path, headers=headers, json=json, params=params)
        body: Any = None
        if res.content:
            body = res.json()
        return Response(status=res.status_code, body=body, request_id=request_id, raw=res)

    def get(self, path: str, user: User | None, **kw: Any) -> Response:
        return self.call("GET", path, user, **kw)

    def post(self, path: str, user: User | None, json: Any) -> Response:
        return self.call("POST", path, user, json=json)

    def patch(self, path: str, user: User | None, json: Any) -> Response:
        return self.call("PATCH", path, user, json=json)

    def put(self, path: str, user: User | None, json: Any) -> Response:
        return self.call("PUT", path, user, json=json)

    def delete(self, path: str, user: User | None) -> Response:
        return self.call("DELETE", path, user)

    # -- setup -------------------------------------------------------------------------
    def create_org(self, owner: User, slug: str | None = None, name: str = "Org") -> uuid.UUID:
        res = self.post("/v1/orgs", owner, {"slug": slug or new_slug(), "name": name})
        assert res.status == 201, res.body
        return uuid.UUID(res.body["id"])

    def set_role(self, org_id: uuid.UUID, actor: User, target: User, role: str) -> None:
        res = self.put(f"/v1/orgs/{org_id}/members/{target.id}", actor, {"role": role})
        assert res.status == 200, res.body

    def tenant(self) -> Tenant:
        owner, admin, member, outsider = (
            self.user("owner"),
            self.user("admin"),
            self.user("member"),
            self.user("outsider"),
        )
        slug = new_slug()
        org_id = self.create_org(owner, slug)
        self.set_role(org_id, owner, admin, "admin")
        self.set_role(org_id, owner, member, "member")
        other = self.create_org(outsider)
        return Tenant(org_id, slug, owner, admin, member, outsider, other)

    def create_workspace(
        self, org_id: uuid.UUID, actor: User, slug: str | None = None
    ) -> uuid.UUID:
        res = self.post(
            f"/v1/orgs/{org_id}/workspaces", actor, {"slug": slug or new_slug("ws"), "name": "WS"}
        )
        assert res.status == 201, res.body
        return uuid.UUID(res.body["id"])

    # -- audit -------------------------------------------------------------------------
    def audit(self, request_id: str) -> list[dict[str, Any]]:
        """Every audit row written for one request (read as admin, bypassing RLS)."""
        with self.admin_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT org_id, actor_user_id, event_type, target_type, target_id, outcome,"
                    " metadata FROM audit_events WHERE request_id = :r ORDER BY occurred_at"
                ),
                {"r": request_id},
            ).mappings()
            return [dict(r) for r in rows]

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.admin_engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def org_role(self, org_id: uuid.UUID, user: User) -> str | None:
        role: str | None = self.scalar(
            "SELECT role FROM organization_memberships WHERE org_id = :o AND user_id = :u",
            o=org_id,
            u=user.id,
        )
        return role


def new_slug(prefix: str = "org") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def assert_denied(api: Api, res: Response, status: int, *, action: str, reason: str) -> None:
    """``res`` was denied with ``status`` and exactly one ``authz.denied`` row was written."""
    expected = {404: {"detail": "not_found"}, 403: {"detail": "forbidden"}}[status]
    assert (res.status, res.body) == (status, expected)
    rows = api.audit(res.request_id)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["event_type"] == "authz.denied"
    assert row["outcome"] == "denied"
    assert row["metadata"] == {"action": action, "reason": reason}


def assert_success_audit(
    api: Api, res: Response, event_type: str, *, actor: User, org_id: uuid.UUID
) -> dict[str, Any]:
    rows = api.audit(res.request_id)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["event_type"] == event_type
    assert row["outcome"] == "success"
    assert row["actor_user_id"] == actor.id
    assert row["org_id"] == org_id
    meta: dict[str, Any] = row["metadata"]
    return meta
