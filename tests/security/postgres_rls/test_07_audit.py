"""``audit_events``: current-org reads only; org-less inserts only by own actor; append-only."""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, admin_count, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

INSERT = (
    "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type, target_id,"
    " outcome) VALUES (:id, :o, :u, 'probe', 'probe', 'x', 'success')"
)


def test_read_only_current_org_events(app_engine: Engine, world: World) -> None:
    with app_tx(app_engine, user=world.a.owner_id, org=world.a.org_id) as conn:
        rows = conn.execute(text("SELECT id, org_id FROM audit_events")).all()
    assert {r.org_id for r in rows} == {world.a.org_id}
    ids = {r.id for r in rows}
    assert world.a.audit_id in ids
    assert not ids & {world.b.audit_id, world.orgless_audit_b_owner, world.orgless_audit_system}


def test_orgless_events_never_readable(app_engine: Engine, world: World) -> None:
    """Even their own actor cannot read org-less events back through the app role."""
    for ctx in (
        {"user": world.b.owner_id},
        {"user": world.b.owner_id, "org": world.b.org_id},
        {},
    ):
        with app_tx(app_engine, **ctx) as conn:
            assert scalar(conn, "SELECT count(*) FROM audit_events WHERE org_id IS NULL") == 0


def test_cross_org_insert_rejected(app_engine: Engine, world: World) -> None:
    expect_denied(
        app_engine,
        INSERT,
        {"id": uuid.uuid4(), "o": world.b.org_id, "u": world.a.owner_id},
        match="row-level security",
        user=world.a.owner_id,
        org=world.a.org_id,
    )


@pytest.mark.parametrize("actor", ["other_user", "b_owner"])
def test_orgless_insert_for_other_actor_rejected(
    app_engine: Engine, world: World, actor: str
) -> None:
    other = world.b.owner_id if actor == "b_owner" else world.a.member_id
    expect_denied(
        app_engine,
        INSERT,
        {"id": uuid.uuid4(), "o": None, "u": other},
        match="row-level security",
        user=world.a.owner_id,
        org=world.a.org_id,
    )


def test_orgless_insert_with_actor_but_no_user_context_rejected(
    app_engine: Engine, world: World
) -> None:
    expect_denied(
        app_engine,
        INSERT,
        {"id": uuid.uuid4(), "o": None, "u": world.a.owner_id},
        match="row-level security",
    )


@pytest.mark.parametrize("actor", ["self", "system"])
def test_orgless_insert_for_own_actor_or_system_allowed(
    app_engine: Engine, world: World, actor: str
) -> None:
    with app_tx(app_engine, user=world.a.owner_id) as conn:
        uid = world.a.owner_id if actor == "self" else None
        r = conn.execute(text(INSERT), {"id": uuid.uuid4(), "o": None, "u": uid})
        assert r.rowcount == 1


def test_insert_returning_orgless_is_refused(app_engine: Engine, world: World) -> None:
    """RETURNING needs the SELECT policy: org-less rows can't be read back (why the model uses
    eager_defaults=False)."""
    expect_denied(
        app_engine,
        INSERT + " RETURNING id",
        {"id": uuid.uuid4(), "o": None, "u": world.a.owner_id},
        match="row-level security",
        user=world.a.owner_id,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET outcome = 'error'",
        "UPDATE audit_events SET org_id = NULL WHERE id = :a",
        "DELETE FROM audit_events",
        "DELETE FROM audit_events WHERE id = :a",
        "TRUNCATE audit_events",
    ],
)
@pytest.mark.parametrize("ctx_name", ["none", "own_org", "other_org"])
def test_app_role_update_delete_always_denied(
    app_engine: Engine, world: World, sql: str, ctx_name: str
) -> None:
    ctx = {
        "none": {},
        "own_org": {"user": world.a.owner_id, "org": world.a.org_id},
        "other_org": {"user": world.a.owner_id, "org": world.b.org_id},
    }[ctx_name]
    expect_denied(app_engine, sql, {"a": world.a.audit_id}, match="permission denied", **ctx)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET outcome = 'error' WHERE id = :a",
        "DELETE FROM audit_events WHERE id = :a",
        "TRUNCATE audit_events",
    ],
)
def test_admin_update_delete_blocked_by_trigger(
    admin_engine: Engine, world: World, sql: str
) -> None:
    with admin_engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(DBAPIError) as info:
                conn.execute(text(sql), {"a": world.a.audit_id})
            assert getattr(info.value.orig, "sqlstate", None) == "42501"
            assert "append-only" in str(info.value.orig)
        finally:
            trans.rollback()
    assert (
        admin_count(
            admin_engine, "SELECT count(*) FROM audit_events WHERE id = :a", {"a": world.a.audit_id}
        )
        == 1
    )
