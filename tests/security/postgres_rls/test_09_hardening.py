"""Regression tests for migration 0002_rls_hardening (DC-016), proposed by this suite's first run."""

from __future__ import annotations

import uuid

from rls_world import World, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

INSERT = (
    "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type, target_id,"
    " outcome) VALUES (:id, :o, :u, 'probe', 'probe', 'x', 'success')"
)


def test_org_audit_insert_cannot_forge_another_actor(app_engine: Engine, world: World) -> None:
    # A's member, in A's context, attributing an event to A's owner -> rejected.
    expect_denied(
        app_engine,
        INSERT,
        {"id": uuid.uuid4(), "o": world.a.org_id, "u": world.a.owner_id},
        match="row-level security",
        user=world.a.member_id,
        org=world.a.org_id,
    )


def test_org_audit_insert_for_self_or_system_allowed(app_engine: Engine, world: World) -> None:
    for actor in (world.a.member_id, None):
        with app_tx(app_engine, user=world.a.member_id, org=world.a.org_id) as conn:
            conn.execute(text(INSERT), {"id": uuid.uuid4(), "o": world.a.org_id, "u": actor})


def test_definer_search_path_puts_pg_temp_last(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        cfg = conn.execute(
            text("SELECT proconfig FROM pg_proc WHERE proname = 'ukos_assert_org_has_owner'")
        ).scalar_one()
    assert cfg == ["search_path=pg_catalog, public, pg_temp"]


def test_user_cannot_repoint_own_identity(app_engine: Engine, world: World) -> None:
    for column in ("issuer", "subject"):
        expect_denied(
            app_engine,
            f"UPDATE users SET {column} = :v WHERE id = :id",
            {"v": f"unclaimed-{uuid.uuid4()}", "id": world.a.owner_id},
            match="immutable",
            user=world.a.owner_id,
            org=world.a.org_id,
        )


def test_non_identity_user_columns_still_updatable(app_engine: Engine, world: World) -> None:
    with app_tx(app_engine, user=world.a.owner_id, org=world.a.org_id) as conn:
        n = conn.execute(
            text("UPDATE users SET display_name = 'Renamed' WHERE id = :id"),
            {"id": world.a.owner_id},
        ).rowcount
        assert n == 1
        name = scalar(
            conn, "SELECT display_name FROM users WHERE id = :id", {"id": world.a.owner_id}
        )
        assert name == "Renamed"
