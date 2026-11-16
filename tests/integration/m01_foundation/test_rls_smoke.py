"""RLS smoke test as the app role (``ukos_app``), plus ``tenant_session`` behaviour.

The full adversarial RLS suite is M01.03.02; this only proves the policies are wired and fail
closed.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from m01_seed import Tenant, new_slug, seed_tenant
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.db import set_identity_context, set_tenant_context, tenant_session
from app.core.models import (
    ORG_SCOPED_TABLES,
    AuditEvent,
    Organization,
    OrganizationMembership,
    User,
)

pytestmark = pytest.mark.usefixtures("migrated")


@pytest.fixture(scope="module")
def tenants(admin_engine: Engine, migrated: None) -> tuple[Tenant, Tenant]:
    return seed_tenant(admin_engine), seed_tenant(admin_engine)


def _count(session_or_conn: object, table: str, org_id: uuid.UUID | None = None) -> int:
    execute = session_or_conn.execute  # type: ignore[attr-defined]
    column = "id" if table == "organizations" else "org_id"
    if org_id is None:
        return int(execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
    return int(
        execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :o"),  # noqa: S608
            {"o": org_id},
        ).scalar_one()
    )


def test_connected_as_app_role_without_bypass(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, r.rolsuper, r.rolbypassrls FROM pg_roles r"
                " WHERE r.rolname = current_user"
            )
        ).one()
    assert tuple(row) == ("ukos_app", False, False)


def test_no_context_sees_nothing(app_engine: Engine, tenants: tuple[Tenant, Tenant]) -> None:
    with tenant_session(user_id=None, org_id=None, engine=app_engine) as session:
        for table in (*ORG_SCOPED_TABLES, "users"):
            assert _count(session, table) == 0, table


def test_org_context_isolates_orgs(app_engine: Engine, tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as session:
        for table in ORG_SCOPED_TABLES:
            assert _count(session, table, a.org_id) >= 1, table
            assert _count(session, table, b.org_id) == 0, table
        visible_users = set(session.scalars(select(User.id)))
        assert a.owner_id in visible_users
        assert b.owner_id not in visible_users


def test_org_list_via_own_membership_with_user_context_only(
    app_engine: Engine, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    with tenant_session(user_id=a.owner_id, org_id=None, engine=app_engine) as session:
        orgs = set(session.scalars(select(Organization.id)))
        assert orgs == {a.org_id}
        memberships = list(session.scalars(select(OrganizationMembership)))
        assert [(m.org_id, m.role) for m in memberships] == [(a.org_id, "owner")]
        # Not org-scoped context: workspaces etc. stay hidden.
        for table in ("workspaces", "workspace_memberships", "resource_grants", "audit_events"):
            assert _count(session, table) == 0, table


def test_cross_org_write_rejected(app_engine: Engine, tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as session,
    ):
        session.execute(
            text(
                "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
                " VALUES (:id, :o, :s, 'evil', :u)"
            ),
            {"id": uuid.uuid4(), "o": b.org_id, "s": new_slug("ws"), "u": a.owner_id},
        )
    assert "row-level security" in str(exc.value.orig)


def test_app_role_create_org_flow(app_engine: Engine) -> None:
    """The POST /v1/orgs shape: provision user, create org + owner + audit in one transaction."""
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    issuer, subject = "http://issuer.test", f"flow-{uuid.uuid4()}"
    with tenant_session(user_id=None, org_id=None, engine=app_engine) as session:
        set_identity_context(session, issuer=issuer, subject=subject)
        session.execute(
            text(
                "INSERT INTO users (id, issuer, subject, email) VALUES (:id, :iss, :sub, :e)"
                " ON CONFLICT (issuer, subject) DO UPDATE SET email = excluded.email,"
                " last_seen_at = now() RETURNING id"
            ),
            {"id": user_id, "iss": issuer, "sub": subject, "e": "flow@example.test"},
        )
        session.add(AuditEvent(
            id=uuid.uuid4(), org_id=None, actor_user_id=None, event_type="user.provisioned",
            target_type="user", target_id=str(user_id), outcome="success", metadata_={},
        ))  # fmt: skip
    # Repeat provisioning (race-safe upsert path) returns the existing id.
    with tenant_session(user_id=None, org_id=None, engine=app_engine) as session:
        set_identity_context(session, issuer=issuer, subject=subject)
        again = session.execute(
            text(
                "INSERT INTO users (id, issuer, subject) VALUES (:id, :iss, :sub)"
                " ON CONFLICT (issuer, subject) DO UPDATE SET last_seen_at = now() RETURNING id"
            ),
            {"id": uuid.uuid4(), "iss": issuer, "sub": subject},
        ).scalar_one()
    assert again == user_id

    with tenant_session(user_id=user_id, org_id=org_id, engine=app_engine) as session:
        session.add(Organization(id=org_id, slug=new_slug(), name="Flow", created_by=user_id))
        session.flush()
        session.add(OrganizationMembership(org_id=org_id, user_id=user_id, role="owner"))
        session.add(AuditEvent(
            id=uuid.uuid4(), org_id=org_id, actor_user_id=user_id, event_type="org.created",
            target_type="org", target_id=str(org_id), outcome="success", metadata_={},
        ))  # fmt: skip
    with tenant_session(user_id=user_id, org_id=None, engine=app_engine) as session:
        assert session.scalar(select(func.count()).select_from(Organization)) == 1
        created = session.scalar(select(Organization.created_at))
        assert created is not None
        assert created.utcoffset() == timedelta(0)  # timestamptz, returned as aware UTC


def test_tenant_session_context_is_transaction_local(
    app_engine: Engine, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as session:
        got = session.execute(
            text(
                "SELECT current_setting('app.current_user_id', true),"
                " current_setting('app.current_org_id', true)"
            )
        ).one()
        assert tuple(got) == (str(a.owner_id), str(a.org_id))
    # Same pool, next transaction: nothing leaks.
    with app_engine.connect() as conn:
        got = conn.execute(
            text(
                "SELECT coalesce(current_setting('app.current_user_id', true), ''),"
                " coalesce(current_setting('app.current_org_id', true), '')"
            )
        ).one()
        assert tuple(got) == ("", "")
        assert _count(conn, "organizations") == 0


def test_tenant_session_rolls_back_on_error(
    app_engine: Engine, admin_engine: Engine, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    slug = new_slug("ws")

    class Boom(Exception):
        pass

    with (
        pytest.raises(Boom),
        tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as session,
    ):
        session.execute(
            text(
                "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
                " VALUES (:id, :o, :s, 'tmp', :u)"
            ),
            {"id": uuid.uuid4(), "o": a.org_id, "s": slug, "u": a.owner_id},
        )
        raise Boom
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM workspaces WHERE slug = :s"), {"s": slug}
            ).scalar_one()
            == 0
        )


def test_context_can_switch_within_transaction(
    app_engine: Engine, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as session:
        assert _count(session, "workspaces", a.org_id) >= 1
        set_tenant_context(session, user_id=a.owner_id, org_id=None)
        assert _count(session, "workspaces") == 0
