"""Audit trail on real PostgreSQL, as the app role under RLS (M01.03.04).

Acceptance: "Membership and role changes create redacted audit records".
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from m01_seed import Tenant, insert_user, seed_tenant, sqlstate
from sqlalchemy import Engine, Table, delete, insert, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import TenantSessionFactory, tenant_session
from app.core.models import OrganizationMembership
from app.modules.audit import (
    REDACTED,
    AuditEventType,
    list_events,
    record_denied,
    record_event,
)

pytestmark = pytest.mark.usefixtures("migrated")

MEMBERS = cast(Table, OrganizationMembership.__table__)


class Boom(Exception):
    pass


@pytest.fixture
def tenant(admin_engine: Engine) -> Tenant:
    return seed_tenant(admin_engine)


@pytest.fixture
def other_user(admin_engine: Engine) -> uuid.UUID:
    with admin_engine.begin() as conn:
        return insert_user(conn)


def _rows(admin_engine: Engine, request_id: str) -> list[dict[str, Any]]:
    """Read back as the admin role (bypasses RLS) to see exactly what was stored."""
    with admin_engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT org_id, actor_user_id, event_type, target_type, target_id, outcome,"
                " request_id, metadata FROM audit_events WHERE request_id = :r"
                " ORDER BY occurred_at, id"
            ),
            {"r": request_id},
        )
        return [dict(row._mapping) for row in result]


def _rid() -> str:
    return f"req-{uuid.uuid4()}"


def test_membership_lifecycle_creates_redacted_audit_records(
    app_engine: Engine, admin_engine: Engine, tenant: Tenant, other_user: uuid.UUID
) -> None:
    org, actor, rid = tenant.org_id, tenant.owner_id, _rid()
    members = MEMBERS
    common: dict[str, Any] = {
        "actor_user_id": actor,
        "org_id": org,
        "target_type": "user",
        "target_id": str(other_user),
        "outcome": "success",
        "request_id": rid,
    }

    with tenant_session(user_id=actor, org_id=org, engine=app_engine) as session:
        session.execute(insert(members).values(org_id=org, user_id=other_user, role="member"))
        record_event(
            session,
            event_type=AuditEventType.ORG_MEMBER_ADDED,
            metadata={
                "email": "new.member@example.com",
                "role": "member",
                "headers": {"Authorization": "Bearer abc", "X-Api-Key": "k-123"},
                "invite_token": "tok-secret",
            },
            **common,
        )
    with tenant_session(user_id=actor, org_id=org, engine=app_engine) as session:
        session.execute(
            update(members)
            .where(members.c.org_id == org, members.c.user_id == other_user)
            .values(role="admin")
        )
        record_event(
            session,
            event_type=AuditEventType.ORG_MEMBER_ROLE_CHANGED,
            metadata={"from": "member", "to": "admin", "refreshToken": "r-1"},
            **common,
        )
    with tenant_session(user_id=actor, org_id=org, engine=app_engine) as session:
        session.execute(
            delete(members).where(members.c.org_id == org, members.c.user_id == other_user)
        )
        record_event(
            session,
            event_type=AuditEventType.ORG_MEMBER_REMOVED,
            metadata={"previous_role": "admin", "password": "hunter2"},
            **common,
        )

    stored = _rows(admin_engine, rid)
    assert [r["event_type"] for r in stored] == [
        "org.member.added",
        "org.member.role_changed",
        "org.member.removed",
    ]
    for row in stored:
        assert row["org_id"] == org
        assert row["actor_user_id"] == actor
        assert (row["target_type"], row["target_id"]) == ("user", str(other_user))
        assert row["outcome"] == "success"
        assert row["request_id"] == rid
    assert stored[0]["metadata"] == {
        "email": "new.member@example.com",
        "role": "member",
        "headers": {"Authorization": REDACTED, "X-Api-Key": REDACTED},
        "invite_token": REDACTED,
    }
    assert stored[1]["metadata"] == {"from": "member", "to": "admin", "refreshToken": REDACTED}
    assert stored[2]["metadata"] == {"previous_role": "admin", "password": REDACTED}
    raw = repr(stored)
    for secret in ("Bearer abc", "k-123", "tok-secret", "r-1", "hunter2"):
        assert secret not in raw

    # The app-role read helper sees them newest first, within the org context.
    with tenant_session(user_id=actor, org_id=org, engine=app_engine) as session:
        listed = [e for e in list_events(session, limit=200) if e.request_id == rid]
    assert [e.event_type for e in listed] == [
        "org.member.removed",
        "org.member.role_changed",
        "org.member.added",
    ]
    assert listed[0].metadata["password"] == REDACTED


def test_rolled_back_transaction_loses_its_event(
    app_engine: Engine, admin_engine: Engine, tenant: Tenant, other_user: uuid.UUID
) -> None:
    rid = _rid()
    members = MEMBERS

    def add_member_then_fail() -> None:
        with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
            s.execute(
                insert(members).values(org_id=tenant.org_id, user_id=other_user, role="member")
            )
            record_event(
                s,
                event_type="org.member.added",
                actor_user_id=tenant.owner_id,
                org_id=tenant.org_id,
                target_type="user",
                target_id=str(other_user),
                outcome="success",
                request_id=rid,
            )
            raise Boom

    with pytest.raises(Boom):
        add_member_then_fail()
    assert _rows(admin_engine, rid) == []
    with admin_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM organization_memberships WHERE org_id = :o AND user_id = :u"
            ),
            {"o": tenant.org_id, "u": other_user},
        ).scalar_one()
    assert count == 0


def test_deferred_constraint_failure_at_commit_also_drops_event(
    app_engine: Engine, admin_engine: Engine, tenant: Tenant
) -> None:
    """Demoting the last owner fails at COMMIT (deferred trigger); the event goes with it."""
    rid = _rid()
    members = MEMBERS

    def demote_last_owner() -> None:
        with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
            s.execute(
                update(members)
                .where(members.c.org_id == tenant.org_id, members.c.user_id == tenant.owner_id)
                .values(role="member")
            )
            record_event(
                s,
                event_type="org.member.role_changed",
                actor_user_id=tenant.owner_id,
                org_id=tenant.org_id,
                target_type="user",
                target_id=str(tenant.owner_id),
                outcome="success",
                request_id=rid,
                metadata={"from": "owner", "to": "member"},
            )

    with pytest.raises(IntegrityError) as excinfo:
        demote_last_owner()
    assert sqlstate(excinfo.value) == "23514"
    assert _rows(admin_engine, rid) == []


@pytest.mark.parametrize("via", ["engine", "factory"])
def test_record_denied_survives_business_rollback(
    app_engine: Engine, admin_engine: Engine, tenant: Tenant, other_user: uuid.UUID, via: str
) -> None:
    rid = _rid()

    def business_transaction_that_fails() -> None:
        with tenant_session(user_id=other_user, org_id=tenant.org_id, engine=app_engine) as s:
            record_event(
                s,
                event_type="org.member.removed",
                actor_user_id=other_user,
                org_id=tenant.org_id,
                target_type="user",
                target_id=str(tenant.owner_id),
                outcome="success",
                request_id=rid,
            )
            raise Boom  # e.g. the policy check failed after work had started

    with pytest.raises(Boom):
        business_transaction_that_fails()

    target = app_engine if via == "engine" else TenantSessionFactory(app_engine)
    record_denied(
        target,
        actor_user_id=other_user,
        org_id=tenant.org_id,
        target_type="org",
        target_id=str(tenant.org_id),
        request_id=rid,
        metadata={"action": "org.members.manage", "reason": "not_org_member", "cookie": "c=1"},
    )

    stored = _rows(admin_engine, rid)
    assert len(stored) == 1
    (row,) = stored
    assert row["event_type"] == "authz.denied"
    assert row["outcome"] == "denied"
    assert row["actor_user_id"] == other_user
    assert row["org_id"] == tenant.org_id
    assert row["metadata"] == {
        "action": "org.members.manage",
        "reason": "not_org_member",
        "cookie": REDACTED,
    }


def test_record_denied_rejects_unknown_type_without_writing(
    app_engine: Engine, admin_engine: Engine, tenant: Tenant
) -> None:
    rid = _rid()
    with pytest.raises(ValueError, match="unknown audit event type"):
        record_denied(
            app_engine,
            actor_user_id=tenant.owner_id,
            org_id=tenant.org_id,
            target_type="org",
            target_id=str(tenant.org_id),
            request_id=rid,
            event_type="authz.maybe",
        )
    assert _rows(admin_engine, rid) == []


def test_orgless_user_provisioned_only_for_current_user(
    app_engine: Engine, admin_engine: Engine, other_user: uuid.UUID
) -> None:
    rid = _rid()
    event: dict[str, Any] = {
        "event_type": AuditEventType.USER_PROVISIONED,
        "org_id": None,
        "target_type": "user",
        "target_id": str(other_user),
        "outcome": "success",
        "request_id": rid,
        "metadata": {"issuer": "http://issuer.test", "email": "p@example.com", "id_token": "x"},
    }
    # Actor == current user: allowed (no RETURNING, so the SELECT policy doesn't interfere).
    with tenant_session(user_id=other_user, org_id=None, engine=app_engine) as s:
        record_event(s, actor_user_id=other_user, **event)
    # System event (no actor): allowed by DC-014.
    with tenant_session(user_id=other_user, org_id=None, engine=app_engine) as s:
        record_event(s, actor_user_id=None, **event)
    # Someone else as actor: rejected by the RLS insert check.
    with (
        pytest.raises(DBAPIError) as excinfo,
        tenant_session(user_id=other_user, org_id=None, engine=app_engine) as s,
    ):
        record_event(s, actor_user_id=uuid.uuid4(), **event)
    assert sqlstate(excinfo.value) == "42501"
    # No user context at all and a claimed actor: rejected.
    with (
        pytest.raises(DBAPIError) as excinfo,
        tenant_session(user_id=None, org_id=None, engine=app_engine) as s,
    ):
        record_event(s, actor_user_id=other_user, **event)
    assert sqlstate(excinfo.value) == "42501"

    stored = _rows(admin_engine, rid)
    assert [(r["actor_user_id"], r["org_id"]) for r in stored] == [(other_user, None), (None, None)]
    assert stored[0]["metadata"]["id_token"] == REDACTED
    assert stored[0]["metadata"]["email"] == "p@example.com"

    # Org-less rows are never readable through the app role (SELECT is current-org only).
    with tenant_session(user_id=other_user, org_id=None, engine=app_engine) as s:
        assert [e for e in list_events(s) if e.request_id == rid] == []


def test_insert_into_foreign_org_rejected(
    app_engine: Engine, admin_engine: Engine, tenant: Tenant
) -> None:
    rid = _rid()
    other = seed_tenant(admin_engine)
    with (
        pytest.raises(DBAPIError) as excinfo,
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
    ):
        record_event(
            s,
            event_type="org.updated",
            actor_user_id=tenant.owner_id,
            org_id=other.org_id,
            target_type="org",
            target_id=str(other.org_id),
            outcome="success",
            request_id=rid,
        )
    assert sqlstate(excinfo.value) == "42501"
    assert _rows(admin_engine, rid) == []


def test_org_b_cannot_read_org_a_events(
    app_engine: Engine, admin_engine: Engine, tenant: Tenant
) -> None:
    org_b = seed_tenant(admin_engine)
    rid = _rid()
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        record_event(
            s,
            event_type="org.updated",
            actor_user_id=tenant.owner_id,
            org_id=tenant.org_id,
            target_type="org",
            target_id=str(tenant.org_id),
            outcome="success",
            request_id=rid,
            metadata={"name": "A"},
        )

    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        assert [e.request_id for e in list_events(s, limit=200)].count(rid) == 1

    with tenant_session(user_id=org_b.owner_id, org_id=org_b.org_id, engine=app_engine) as s:
        visible = list_events(s, limit=200)
        assert visible, "org B sees its own seeded event"
        assert {e.org_id for e in visible} == {org_b.org_id}
        assert rid not in {e.request_id for e in visible}
        leaked = s.execute(
            text("SELECT count(*) FROM audit_events WHERE org_id = :a"), {"a": tenant.org_id}
        ).scalar_one()
        assert leaked == 0

    # Same user, no org context: nothing (fail closed).
    with tenant_session(user_id=tenant.owner_id, org_id=None, engine=app_engine) as s:
        assert list_events(s) == []


def test_list_events_pages_with_cursor(app_engine: Engine, tenant: Tenant) -> None:
    rid = _rid()
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        for i in range(5):
            record_event(
                s,
                event_type="org.updated",
                actor_user_id=tenant.owner_id,
                org_id=tenant.org_id,
                target_type="org",
                target_id=str(tenant.org_id),
                outcome="success",
                request_id=rid,
                metadata={"n": i},
            )
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        everything = list_events(s, limit=200)
        pages: list[int] = []
        cursor = None
        while True:
            page = list_events(s, limit=2, before=cursor)
            if not page:
                break
            pages.extend(e.metadata.get("n", -1) for e in page if e.request_id == rid)
            cursor = page[-1].cursor
    assert pages == [4, 3, 2, 1, 0]
    assert len(everything) == 6  # 5 new + the seeded org.created


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET outcome = 'error' WHERE org_id = :o",
        "DELETE FROM audit_events WHERE org_id = :o",
        "TRUNCATE audit_events",
    ],
)
def test_audit_events_are_append_only_for_app_role(
    app_engine: Engine, tenant: Tenant, statement: str
) -> None:
    with (
        pytest.raises(DBAPIError) as excinfo,
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
    ):
        s.execute(text(statement), {"o": tenant.org_id})
    assert sqlstate(excinfo.value) == "42501"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET outcome = 'error' WHERE org_id = :o",
        "DELETE FROM audit_events WHERE org_id = :o",
    ],
)
def test_audit_events_are_append_only_even_for_admin(
    admin_engine: Engine, tenant: Tenant, statement: str
) -> None:
    with pytest.raises(DBAPIError) as excinfo, admin_engine.begin() as conn:
        conn.execute(text(statement), {"o": tenant.org_id})
    assert sqlstate(excinfo.value) == "42501"
