"""SECURITY DEFINER functions: pinned search_path, not callable by the app role, not abusable."""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, admin_count, app_tx, expect_denied
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError


def _definers(admin: Engine) -> list[tuple[str, list[str] | None, str]]:
    with admin.connect() as conn:
        return [
            (r.proname, r.proconfig, r.owner)
            for r in conn.execute(
                text(
                    "SELECT p.proname, p.proconfig, pg_get_userbyid(p.proowner) AS owner"
                    " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
                )
            )
        ]


def test_expected_definer_inventory(admin_engine: Engine, migrated: None) -> None:
    assert sorted(name for name, _, _ in _definers(admin_engine)) == ["ukos_assert_org_has_owner"]


def test_definers_pin_search_path(admin_engine: Engine, migrated: None) -> None:
    for name, config, _owner in _definers(admin_engine):
        paths = [c for c in (config or []) if c.startswith("search_path=")]
        assert paths, f"{name}: SECURITY DEFINER without pinned search_path"
        entries = [e.strip() for e in paths[0].split("=", 1)[1].split(",")]
        assert entries[0] == "pg_catalog", name
        assert "$user" not in entries and '"$user"' not in entries, name


def test_definers_not_executable_by_app_role_or_public(
    admin_engine: Engine, migrated: None, app_role: str
) -> None:
    with admin_engine.connect() as conn:
        for name, _, _ in _definers(admin_engine):
            sig = f"public.{name}()"
            assert (
                conn.execute(
                    text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                    {"r": app_role, "f": sig},
                ).scalar_one()
                is False
            ), name
            acl = conn.execute(
                text("SELECT proacl::text FROM pg_proc WHERE oid = CAST(:f AS regprocedure)"),
                {"f": sig},
            ).scalar_one()
            # NULL acl = default = PUBLIC may EXECUTE; a PUBLIC entry is an item "=X/<grantor>".
            assert acl is not None, f"{name}: default ACL grants PUBLIC EXECUTE"
            items = acl.strip("{}").split(",")
            assert not any(i.startswith("=") for i in items), f"{name}: PUBLIC entry in {acl}"


def test_app_role_cannot_call_definer_directly(app_engine: Engine, world: World) -> None:
    expect_denied(
        app_engine,
        "SELECT public.ukos_assert_org_has_owner()",
        match="permission denied",
        user=world.a.owner_id,
        org=world.b.org_id,
    )


def test_app_role_cannot_replace_definer(app_engine: Engine) -> None:
    expect_denied(
        app_engine,
        "CREATE OR REPLACE FUNCTION public.ukos_assert_org_has_owner() RETURNS trigger"
        " LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$",
    )
    expect_denied(app_engine, "ALTER FUNCTION public.ukos_assert_org_has_owner() RESET ALL")


def test_definer_trigger_still_enforces_owner_with_shadow_temp_tables(
    app_engine: Engine, world: World
) -> None:
    """pg_temp comes first for relations when not listed in search_path. Empty temp
    'organizations' / 'organization_memberships' must not fool the definer (its body uses
    schema-qualified names): removing A's only owner still fails at COMMIT with 23514."""
    a = world.a
    with app_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text(
                    "SELECT set_config('app.current_user_id', :u, true),"
                    " set_config('app.current_org_id', :o, true)"
                ),
                {"u": str(a.owner_id), "o": str(a.org_id)},
            )
            conn.execute(text("CREATE TEMP TABLE organizations (id uuid, deleted_at timestamptz)"))
            conn.execute(
                text("CREATE TEMP TABLE organization_memberships (org_id uuid, role text)")
            )
            r = conn.execute(
                text("UPDATE public.organization_memberships SET role = 'admin'"
                     " WHERE org_id = :o AND user_id = :u"),
                {"o": a.org_id, "u": a.owner_id},
            )  # fmt: skip
            assert r.rowcount == 1
            with pytest.raises(DBAPIError) as info:
                trans.commit()
            assert getattr(info.value.orig, "sqlstate", None) == "23514"
            assert "last_owner" in str(info.value.orig)
        finally:
            if trans.is_active:
                trans.rollback()


def test_definer_trigger_is_not_a_cross_org_oracle(
    app_engine: Engine, admin_engine: Engine, world: World
) -> None:
    """From A, attempts on B's owner never reach the definer: 0 rows, commit succeeds, B intact."""
    b = world.b
    with app_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "SELECT set_config('app.current_user_id', :u, true),"
                " set_config('app.current_org_id', :o, true)"
            ),
            {"u": str(world.a.owner_id), "o": str(world.a.org_id)},
        )
        for sql in (
            "DELETE FROM organization_memberships WHERE org_id = :b",
            "UPDATE organization_memberships SET role = 'member' WHERE org_id = :b",
            "UPDATE organizations SET deleted_at = NULL WHERE id = :b",
        ):
            assert conn.execute(text(sql), {"b": b.org_id}).rowcount == 0, sql
        # committed without error
    assert (
        admin_count(
            admin_engine,
            "SELECT count(*) FROM organization_memberships WHERE org_id = :b AND role = 'owner'",
            {"b": b.org_id},
        )
        == 1
    )


def test_owner_invariant_holds_for_org_insert_without_owner(
    app_engine: Engine, world: World
) -> None:
    """The definer sees through RLS correctly: creating an org with no owner fails at commit."""
    with app_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("SELECT set_config('app.current_user_id', :u, true)"),
                {"u": str(world.outsider_id)},
            )
            conn.execute(
                text("INSERT INTO organizations (id, slug, name, created_by)"
                     " VALUES (:id, :s, 'x', :u)"),
                {"id": uuid.uuid4(), "s": f"noowner-{uuid.uuid4().hex[:10]}",
                 "u": world.outsider_id},
            )  # fmt: skip
            with pytest.raises(DBAPIError) as info:
                trans.commit()
            assert getattr(info.value.orig, "sqlstate", None) == "23514"
        finally:
            if trans.is_active:
                trans.rollback()


def test_app_tx_helper_rolls_back(app_engine: Engine, admin_engine: Engine, world: World) -> None:
    """Sanity for the harness itself: attacks never persist."""
    marker = f"harness-{uuid.uuid4().hex[:8]}"
    with app_tx(app_engine, user=world.a.owner_id, org=world.a.org_id) as conn:
        conn.execute(
            text("UPDATE workspaces SET name = :n WHERE id = :w"),
            {"n": marker, "w": world.a.workspace_id},
        )
    assert (
        admin_count(admin_engine, "SELECT count(*) FROM workspaces WHERE name = :n", {"n": marker})
        == 0
    )
