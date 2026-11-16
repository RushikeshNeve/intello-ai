"""Seed data, table specs and app-role helpers for the RLS suite.

Seeding runs as the admin role (a superuser, so it bypasses RLS). Attacks run as ``ukos_app``
through :func:`app_tx`, which opens a transaction, sets the RLS context exactly like
``app.core.db`` does (``set_config(..., true)``) and ALWAYS rolls back, so attacks never mutate
the shared world.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

ISSUER = "http://issuer.test"


def new_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Tenant:
    """One org with an owner, a plain member, one workspace and one grant."""

    label: str
    org_id: uuid.UUID
    slug: str
    owner_id: uuid.UUID
    member_id: uuid.UUID
    workspace_id: uuid.UUID
    grant_id: uuid.UUID
    audit_id: uuid.UUID
    marker: str  # appears in names / event types, to detect leaks in free text


@dataclass(frozen=True)
class World:
    a: Tenant
    b: Tenant
    shared_id: uuid.UUID  # member of both A and B
    outsider_id: uuid.UUID  # member of nothing
    orgless_audit_b_owner: uuid.UUID  # org-less audit event acted by B's owner
    orgless_audit_system: uuid.UUID  # org-less audit event without actor
    subjects: dict[uuid.UUID, str] = field(default_factory=dict)

    def subject(self, user_id: uuid.UUID) -> str:
        return self.subjects[user_id]


def _user(conn: Connection, subjects: dict[uuid.UUID, str], label: str) -> uuid.UUID:
    uid = uuid.uuid4()
    sub = f"{label}-{uid}"
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, email, display_name)"
            " VALUES (:id, :iss, :sub, :email, :dn)"
        ),
        {"id": uid, "iss": ISSUER, "sub": sub, "email": f"{label}@example.test", "dn": label},
    )
    subjects[uid] = sub
    return uid


def _tenant(conn: Connection, subjects: dict[uuid.UUID, str], label: str) -> Tenant:
    marker = f"secret-{label}-{uuid.uuid4().hex[:8]}"
    owner = _user(conn, subjects, f"{label}-owner")
    member = _user(conn, subjects, f"{label}-member")
    org, ws, grant, audit = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    slug = new_slug(f"org-{label}")
    conn.execute(
        text("INSERT INTO organizations (id, slug, name, created_by) VALUES (:id, :s, :n, :u)"),
        {"id": org, "s": slug, "n": f"{marker} org", "u": owner},
    )
    conn.execute(
        text(
            "INSERT INTO organization_memberships (org_id, user_id, role)"
            " VALUES (:o, :owner, 'owner'), (:o, :member, 'member')"
        ),
        {"o": org, "owner": owner, "member": member},
    )
    conn.execute(
        text(
            "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
            " VALUES (:id, :o, :s, :n, :u)"
        ),
        {"id": ws, "o": org, "s": new_slug("ws"), "n": f"{marker} ws", "u": owner},
    )
    conn.execute(
        text(
            "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
            " VALUES (:w, :o, :owner, 'admin'), (:w, :o, :member, 'viewer')"
        ),
        {"w": ws, "o": org, "owner": owner, "member": member},
    )
    conn.execute(
        text(
            "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type, resource_id,"
            " grantee_type, grantee_id, permission, granted_by)"
            " VALUES (:id, :o, :w, 'document', :r, 'user', :g, 'read', :u)"
        ),
        {"id": grant, "o": org, "w": ws, "r": uuid.uuid4(), "g": str(member), "u": owner},
    )
    conn.execute(
        text(
            "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type,"
            " target_id, outcome, metadata)"
            " VALUES (:id, :o, :u, 'org.created', 'org', :t, 'success',"
            " jsonb_build_object('marker', CAST(:m AS text)))"
        ),
        {"id": audit, "o": org, "u": owner, "t": str(org), "m": marker},
    )
    return Tenant(
        label=label,
        org_id=org,
        slug=slug,
        owner_id=owner,
        member_id=member,
        workspace_id=ws,
        grant_id=grant,
        audit_id=audit,
        marker=marker,
    )


def seed_world(engine: Engine) -> World:
    subjects: dict[uuid.UUID, str] = {}
    with engine.begin() as conn:
        a = _tenant(conn, subjects, "a")
        b = _tenant(conn, subjects, "b")
        shared = _user(conn, subjects, "shared")
        outsider = _user(conn, subjects, "outsider")
        conn.execute(
            text(
                "INSERT INTO organization_memberships (org_id, user_id, role)"
                " VALUES (:a, :u, 'member'), (:b, :u, 'member')"
            ),
            {"a": a.org_id, "b": b.org_id, "u": shared},
        )
        orgless_b, orgless_sys = uuid.uuid4(), uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type,"
                " target_id, outcome) VALUES"
                " (:x, NULL, :bu, 'user.provisioned', 'user', :bt, 'success'),"
                " (:y, NULL, NULL, 'system.probe', 'system', 'x', 'success')"
            ),
            {"x": orgless_b, "bu": b.owner_id, "bt": str(b.owner_id), "y": orgless_sys},
        )
        # Planner statistics exist for every table (pg_stats must still not expose them).
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE"))
    return World(
        a=a,
        b=b,
        shared_id=shared,
        outsider_id=outsider,
        orgless_audit_b_owner=orgless_b,
        orgless_audit_system=orgless_sys,
        subjects=subjects,
    )


# --------------------------------------------------------------------------------------------
# Table specs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    name: str
    org_col: str  # column carrying the org id
    key_expr: str  # textual primary key, for "lookup by PK" attacks
    # A harmless UPDATE assignment (None = the app role holds no UPDATE privilege).
    update_set: str | None
    # SET clause that moves a row to another org (bind :other); None if not applicable.
    move_set: str | None
    has_delete_grant: bool
    insert: Callable[[World, Tenant], tuple[str, dict[str, Any]]]


def _ins_org(w: World, t: Tenant) -> tuple[str, dict[str, Any]]:
    # An org row "for tenant t" means id = t.org_id is impossible (PK exists); org-scoping for
    # organizations is by id, so the attack is a NEW org row created_by t's owner.
    return (
        "INSERT INTO organizations (id, slug, name, created_by) VALUES (:id, :s, 'evil', :u)",
        {"id": uuid.uuid4(), "s": new_slug("evil"), "u": t.owner_id},
    )


def _ins_om(w: World, t: Tenant) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO organization_memberships (org_id, user_id, role) VALUES (:o, :u, 'admin')",
        {"o": t.org_id, "u": w.outsider_id},
    )


def _ins_ws(w: World, t: Tenant) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
        " VALUES (:id, :o, :s, 'evil', :u)",
        {"id": uuid.uuid4(), "o": t.org_id, "s": new_slug("evil"), "u": t.owner_id},
    )


def _ins_wm(w: World, t: Tenant) -> tuple[str, dict[str, Any]]:
    # shared user is an org member of both orgs, so the row is FK-valid for either tenant.
    return (
        "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
        " VALUES (:w, :o, :u, 'admin')",
        {"w": t.workspace_id, "o": t.org_id, "u": w.shared_id},
    )


def _ins_rg(w: World, t: Tenant) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type, resource_id,"
        " grantee_type, grantee_id, permission, granted_by)"
        " VALUES (:id, :o, :w, 'document', :r, 'workspace_role', 'viewer', 'manage', :u)",
        {"id": uuid.uuid4(), "o": t.org_id, "w": t.workspace_id, "r": uuid.uuid4(),
         "u": t.owner_id},
    )  # fmt: skip


def _ins_ae(w: World, t: Tenant) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type,"
        " target_id, outcome) VALUES (:id, :o, :u, 'evil.forged', 'org', 'x', 'success')",
        {"id": uuid.uuid4(), "o": t.org_id, "u": t.owner_id},
    )


ORG_TABLE_SPECS: dict[str, TableSpec] = {
    s.name: s
    for s in (
        TableSpec("organizations", "id", "id::text", "name = 'pwned'",
                  "id = CAST(:other AS uuid)", False, _ins_org),
        TableSpec("organization_memberships", "org_id", "org_id::text || '/' || user_id::text",
                  "role = 'admin'", "org_id = CAST(:other AS uuid)", True, _ins_om),
        TableSpec("workspaces", "org_id", "id::text", "name = 'pwned'",
                  "org_id = CAST(:other AS uuid)", False, _ins_ws),
        TableSpec("workspace_memberships", "org_id",
                  "workspace_id::text || '/' || user_id::text", "role = 'editor'",
                  "org_id = CAST(:other AS uuid)", True, _ins_wm),
        TableSpec("resource_grants", "org_id", "id::text", "permission = 'manage'",
                  "org_id = CAST(:other AS uuid)", False, _ins_rg),
        TableSpec("audit_events", "org_id", "id::text", None, None, False, _ins_ae),
    )
}  # fmt: skip


# --------------------------------------------------------------------------------------------
# App-role helpers
# --------------------------------------------------------------------------------------------


def set_ctx(
    conn: Connection,
    *,
    user: uuid.UUID | None = None,
    org: uuid.UUID | None = None,
    issuer: str | None = None,
    subject: str | None = None,
) -> None:
    """Transaction-local context, identical to app.core.db.set_tenant_context/identity."""
    conn.execute(
        text(
            "SELECT set_config('app.current_user_id', :u, true),"
            " set_config('app.current_org_id', :o, true),"
            " set_config('app.current_issuer', :i, true),"
            " set_config('app.current_subject', :s, true)"
        ),
        {
            "u": "" if user is None else str(user),
            "o": "" if org is None else str(org),
            "i": issuer or "",
            "s": subject or "",
        },
    )


@contextmanager
def app_tx(engine: Engine, **ctx: Any) -> Iterator[Connection]:
    """App-role transaction with context; always rolled back."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            if ctx:
                set_ctx(conn, **ctx)
            yield conn
        finally:
            if trans.is_active:
                trans.rollback()


def scalar(conn: Connection, sql: str, params: dict[str, Any] | None = None) -> Any:
    return conn.execute(text(sql), params or {}).scalar()


def sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", None)


def expect_denied(
    engine: Engine,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    states: tuple[str, ...] = ("42501",),
    match: str | None = None,
    **ctx: Any,
) -> DBAPIError:
    """Run ``sql`` as the app role in context; it must fail with one of ``states``."""
    with pytest.raises(DBAPIError) as info, app_tx(engine, **ctx) as conn:
        conn.execute(text(sql), params or {})
    assert sqlstate(info.value) in states, f"{sqlstate(info.value)}: {info.value.orig}"
    if match is not None:
        assert match in str(info.value.orig), str(info.value.orig)
    return info.value


def admin_count(admin: Engine, sql: str, params: dict[str, Any] | None = None) -> int:
    with admin.connect() as conn:
        return int(conn.execute(text(sql), params or {}).scalar_one())


def tenant_keys(admin: Engine, spec: TableSpec, org_id: uuid.UUID) -> list[str]:
    with admin.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {spec.key_expr} FROM {spec.name} WHERE {spec.org_col} = :o"),  # noqa: S608
            {"o": org_id},
        ).scalars()
        keys = [str(k) for k in rows]
    assert keys, f"seed has no rows in {spec.name} for {org_id}"
    return keys
