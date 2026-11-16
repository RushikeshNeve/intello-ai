# ruff: noqa: S608  (SQL is assembled from the fixed table specs in rls_world, never input)
"""Org A context: every read path shows only A; every write path towards B is a no-op or rejected.

Context for every test: ``user = A.owner, org = A`` (what the API sets for an org-A request).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.db import tenant_session
from rls_world import (
    ORG_TABLE_SPECS,
    World,
    admin_count,
    app_tx,
    expect_denied,
    scalar,
    set_ctx,
    sqlstate,
    tenant_keys,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

TABLES = list(ORG_TABLE_SPECS)


def ctx_a(world: World) -> dict[str, Any]:
    return {"user": world.a.owner_id, "org": world.a.org_id}


# -- reads ------------------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_only_own_org_rows_visible(app_engine: Engine, world: World, table: str) -> None:
    spec = ORG_TABLE_SPECS[table]
    with app_tx(app_engine, **ctx_a(world)) as conn:
        orgs = set(conn.execute(text(f"SELECT DISTINCT {spec.org_col} FROM {table}")).scalars())
    assert orgs == {world.a.org_id}, table


@pytest.mark.parametrize("table", TABLES)
def test_visible_count_equals_own_org_count(
    app_engine: Engine, admin_engine: Engine, world: World, table: str
) -> None:
    spec = ORG_TABLE_SPECS[table]
    expected = admin_count(
        admin_engine,
        f"SELECT count(*) FROM {table} WHERE {spec.org_col} = :o",
        {"o": world.a.org_id},
    )
    with tenant_session(user_id=world.a.owner_id, org_id=world.a.org_id, engine=app_engine) as s:
        got = s.execute(text(f"SELECT count(*) FROM {table}")).scalar()
    assert got == expected


@pytest.mark.parametrize("table", TABLES)
def test_primary_key_lookup_of_b_rows_returns_nothing(
    app_engine: Engine, admin_engine: Engine, world: World, table: str
) -> None:
    spec = ORG_TABLE_SPECS[table]
    keys = tenant_keys(admin_engine, spec, world.b.org_id)
    with app_tx(app_engine, **ctx_a(world)) as conn:
        for key in keys:
            sql = f"SELECT count(*) FROM {table} WHERE {spec.key_expr} = :k"
            assert scalar(conn, sql, {"k": key}) == 0
        sql = f"SELECT count(*) FROM {table} WHERE {spec.key_expr} = ANY(:ks)"
        assert scalar(conn, sql, {"ks": keys}) == 0


def test_direct_id_lookups_of_b(app_engine: Engine, world: World) -> None:
    b = world.b
    probes = [
        ("SELECT * FROM organizations WHERE id = :i", b.org_id),
        ("SELECT * FROM organizations WHERE slug = :i", b.slug),
        ("SELECT * FROM workspaces WHERE id = :i", b.workspace_id),
        ("SELECT * FROM resource_grants WHERE id = :i", b.grant_id),
        ("SELECT * FROM audit_events WHERE id = :i", b.audit_id),
        ("SELECT * FROM organization_memberships WHERE user_id = :i", b.owner_id),
        ("SELECT * FROM workspace_memberships WHERE workspace_id = :i", b.workspace_id),
        ("SELECT * FROM users WHERE id = :i", b.owner_id),
        ("SELECT * FROM audit_events WHERE id = :i", world.orgless_audit_b_owner),
    ]
    with app_tx(app_engine, **ctx_a(world)) as conn:
        for sql, value in probes:
            assert conn.execute(text(sql), {"i": value}).all() == [], sql


@pytest.mark.parametrize("table", TABLES)
def test_aggregates_reveal_nothing(app_engine: Engine, world: World, table: str) -> None:
    col = ORG_TABLE_SPECS[table].org_col
    with app_tx(app_engine, **ctx_a(world)) as conn:
        row = conn.execute(
            text(
                f"SELECT count(DISTINCT {col}), bool_and({col} = :a),"
                f" count(*) FILTER (WHERE {col} = :b),"
                f" EXISTS (SELECT 1 FROM {table} WHERE {col} = :b),"
                f" (SELECT max({col}::text) FROM {table} WHERE {col} = :b)"
                f" FROM {table}"
            ),
            {"a": world.a.org_id, "b": world.b.org_id},
        ).one()
    assert tuple(row) == (1, True, 0, False, None)


@pytest.mark.parametrize("table", TABLES)
def test_window_functions_reveal_nothing(
    app_engine: Engine, admin_engine: Engine, world: World, table: str
) -> None:
    col = ORG_TABLE_SPECS[table].org_col
    a_count = admin_count(
        admin_engine,
        f"SELECT count(*) FROM {table} WHERE {col} = :o",
        {"o": world.a.org_id},
    )
    with app_tx(app_engine, **ctx_a(world)) as conn:
        rows = conn.execute(
            text(
                f"SELECT count(*) OVER (), row_number() OVER (ORDER BY {col}),"
                f" dense_rank() OVER (ORDER BY {col}),"
                f" count(*) OVER (PARTITION BY {col}) FROM {table}"
            )
        ).all()
    assert rows
    assert {r[0] for r in rows} == {a_count}
    assert max(r[1] for r in rows) == a_count
    assert {r[2] for r in rows} == {1}  # one org only
    assert {r[3] for r in rows} == {a_count}


@pytest.mark.parametrize("table", TABLES)
def test_ctes_reveal_nothing(app_engine: Engine, world: World, table: str) -> None:
    col = ORG_TABLE_SPECS[table].org_col
    with app_tx(app_engine, **ctx_a(world)) as conn:
        n = scalar(
            conn,
            f"WITH everything AS MATERIALIZED (SELECT * FROM {table}),"
            f" again AS (SELECT * FROM everything UNION ALL SELECT * FROM {table})"
            f" SELECT count(*) FROM again WHERE {col} = :b",
            {"b": world.b.org_id},
        )
        assert n == 0
        n = scalar(
            conn,
            "WITH RECURSIVE r(n, o) AS ("
            f" SELECT 1, {col} FROM {table}"
            f" UNION ALL SELECT n + 1, o FROM r WHERE n < 3)"
            " SELECT count(*) FROM r WHERE o = :b",
            {"b": world.b.org_id},
        )
        assert n == 0


def test_joins_and_subqueries_reveal_nothing(app_engine: Engine, world: World) -> None:
    b = world.b
    probes = [
        # Join from own rows into B ids.
        "SELECT count(*) FROM workspaces w JOIN workspace_memberships m"
        " ON m.workspace_id = w.id WHERE m.org_id = :b OR w.org_id = :b",
        # Cross join every table pair; B never appears.
        "SELECT count(*) FROM organizations o, workspaces w, resource_grants g"
        " WHERE :b IN (o.id, w.org_id, g.org_id)",
        # Correlated / scalar subqueries.
        "SELECT count(*) FROM organizations o WHERE EXISTS"
        " (SELECT 1 FROM workspaces w WHERE w.org_id = :b)",
        "SELECT coalesce((SELECT count(*) FROM resource_grants WHERE org_id = :b), 0)",
        "SELECT count(*) FROM (SELECT org_id FROM organization_memberships"
        " UNION SELECT org_id FROM workspace_memberships"
        " UNION SELECT org_id FROM audit_events) u WHERE org_id = :b",
        # LATERAL.
        "SELECT count(*) FROM organizations o CROSS JOIN LATERAL"
        " (SELECT * FROM workspaces w WHERE w.org_id <> o.id) x",
        # Users joined through memberships of B.
        "SELECT count(*) FROM users u JOIN organization_memberships m ON m.user_id = u.id"
        " WHERE m.org_id = :b",
        # Text search over every visible column for B's marker.
        "SELECT count(*) FROM (SELECT row_to_json(t)::text AS j FROM organizations t"
        " UNION ALL SELECT row_to_json(t)::text FROM workspaces t"
        " UNION ALL SELECT row_to_json(t)::text FROM audit_events t"
        " UNION ALL SELECT row_to_json(t)::text FROM users t) s WHERE j LIKE :mk",
    ]
    with app_tx(app_engine, **ctx_a(world)) as conn:
        for sql in probes:
            n = scalar(conn, sql, {"b": b.org_id, "mk": f"%{b.marker}%"})
            assert n == 0, sql


def test_json_and_array_aggregation_contains_no_b_data(app_engine: Engine, world: World) -> None:
    with app_tx(app_engine, **ctx_a(world)) as conn:
        dump = scalar(
            conn,
            "SELECT concat_ws('|',"
            " (SELECT json_agg(t)::text FROM organizations t),"
            " (SELECT json_agg(t)::text FROM organization_memberships t),"
            " (SELECT json_agg(t)::text FROM workspaces t),"
            " (SELECT json_agg(t)::text FROM workspace_memberships t),"
            " (SELECT json_agg(t)::text FROM resource_grants t),"
            " (SELECT json_agg(t)::text FROM audit_events t),"
            " (SELECT json_agg(t)::text FROM users t))",
        )
    assert world.a.marker in dump
    for needle in (
        world.b.marker,
        str(world.b.org_id),
        str(world.b.owner_id),
        str(world.b.member_id),
        str(world.b.workspace_id),
        world.b.slug,
    ):
        assert needle not in dump, needle  # fmt: skip


# -- writes -----------------------------------------------------------------------------------


@pytest.mark.parametrize("table", [t for t, s in ORG_TABLE_SPECS.items() if s.update_set])
def test_update_b_rows_by_key_affects_nothing(
    app_engine: Engine, admin_engine: Engine, world: World, table: str
) -> None:
    spec = ORG_TABLE_SPECS[table]
    keys = tenant_keys(admin_engine, spec, world.b.org_id)
    with app_tx(app_engine, **ctx_a(world)) as conn:
        r = conn.execute(
            text(f"UPDATE {table} SET {spec.update_set} WHERE {spec.key_expr} = ANY(:ks)"),
            {"ks": keys},
        )
        assert r.rowcount == 0
        # Unqualified UPDATE only touches A rows.
        r = conn.execute(text(f"UPDATE {table} SET {spec.update_set} RETURNING {spec.org_col}"))
        assert {row[0] for row in r} == {world.a.org_id}


def test_update_b_by_id_explicit(app_engine: Engine, world: World) -> None:
    b = world.b
    with app_tx(app_engine, **ctx_a(world)) as conn:
        for sql, value in (
            ("UPDATE organizations SET name = 'pwned' WHERE id = :i", b.org_id),
            ("UPDATE organizations SET deleted_at = now() WHERE id = :i", b.org_id),
            ("UPDATE workspaces SET name = 'pwned' WHERE id = :i", b.workspace_id),
            ("UPDATE resource_grants SET revoked_at = now() WHERE id = :i", b.grant_id),
            ("UPDATE organization_memberships SET role = 'member' WHERE user_id = :i",
             b.owner_id),
            ("UPDATE users SET display_name = 'pwned' WHERE id = :i", b.owner_id),
        ):  # fmt: skip
            assert conn.execute(text(sql), {"i": value}).rowcount == 0, sql


@pytest.mark.parametrize("table", ["organization_memberships", "workspace_memberships"])
def test_delete_b_rows_affects_nothing(
    app_engine: Engine, admin_engine: Engine, world: World, table: str
) -> None:
    before = admin_count(
        admin_engine,
        f"SELECT count(*) FROM {table} WHERE org_id = :o",
        {"o": world.b.org_id},
    )
    with app_tx(app_engine, **ctx_a(world)) as conn:
        assert (
            conn.execute(
                text(f"DELETE FROM {table} WHERE org_id = :o"),
                {"o": world.b.org_id},
            ).rowcount
            == 0
        )
        assert (
            conn.execute(
                text(f"DELETE FROM {table} WHERE user_id = :u"),
                {"u": world.b.owner_id},
            ).rowcount
            == 0
        )
    after = admin_count(
        admin_engine,
        f"SELECT count(*) FROM {table} WHERE org_id = :o",
        {"o": world.b.org_id},
    )
    assert after == before


@pytest.mark.parametrize("table", TABLES)
def test_insert_row_for_b_rejected_by_with_check(
    app_engine: Engine, world: World, table: str
) -> None:
    sql, params = ORG_TABLE_SPECS[table].insert(world, world.b)
    expect_denied(app_engine, sql, params, match="row-level security", **ctx_a(world))


@pytest.mark.parametrize("table", TABLES)
def test_insert_row_for_own_org_allowed(app_engine: Engine, world: World, table: str) -> None:
    """Positive control: the same statement for A succeeds (so the B rejection is RLS)."""
    sql, params = ORG_TABLE_SPECS[table].insert(world, world.a)
    with app_tx(app_engine, **ctx_a(world)) as conn:
        assert conn.execute(text(sql), params).rowcount == 1


@pytest.mark.parametrize("table", [t for t, s in ORG_TABLE_SPECS.items() if s.move_set])
def test_moving_own_row_into_b_rejected(app_engine: Engine, world: World, table: str) -> None:
    spec = ORG_TABLE_SPECS[table]
    target = world.b.org_id if table != "organizations" else uuid.uuid4()
    expect_denied(
        app_engine,
        f"UPDATE {table} SET {spec.move_set} WHERE {spec.org_col} = :a",
        {"other": str(target), "a": world.a.org_id},
        match="row-level security",
        **ctx_a(world),
    )


def test_upsert_on_conflict_cannot_touch_b(app_engine: Engine, world: World) -> None:
    """INSERT .. ON CONFLICT DO UPDATE aimed at an existing B row: the proposed row fails the
    INSERT check, and the conflicting B row is invisible to the UPDATE policy."""
    b = world.b
    expect_denied(
        app_engine,
        "INSERT INTO organization_memberships (org_id, user_id, role) VALUES (:o, :u, 'member')"
        " ON CONFLICT (org_id, user_id) DO UPDATE SET role = 'member'",
        {"o": b.org_id, "u": b.owner_id},
        match="row-level security",
        **ctx_a(world),
    )
    # Conflict on a B primary key from an A-valid row: the existing B row is not updatable.
    expect_denied(
        app_engine,
        "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
        " VALUES (:id, :a, 'x-upsert', 'x', :u)"
        " ON CONFLICT (id) DO UPDATE SET name = 'pwned'",
        {"id": b.workspace_id, "a": world.a.org_id, "u": world.a.owner_id},
        match="row-level security",
        **ctx_a(world),
    )


# -- side channels ----------------------------------------------------------------------------


def test_copy_to_respects_rls_and_copy_from_refused(app_engine: Engine, world: World) -> None:
    raw = app_engine.raw_connection()
    try:
        pg = raw.driver_connection
        assert pg is not None
        with pg.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.current_user_id', %s, true),"
                " set_config('app.current_org_id', %s, true)",
                (str(world.a.owner_id), str(world.a.org_id)),
            )
            out = bytearray()
            with cur.copy("COPY workspaces TO STDOUT") as copy:
                for chunk in copy:
                    out += chunk
            dump = out.decode()
            assert world.a.marker in dump
            assert world.b.marker not in dump and str(world.b.org_id) not in dump
            out.clear()
            with cur.copy("COPY (SELECT * FROM users) TO STDOUT") as copy:
                for chunk in copy:
                    out += chunk
            assert str(world.b.owner_id) not in out.decode()
        pg.rollback()
        with pg.cursor() as cur, pytest.raises(Exception) as exc:
            with cur.copy("COPY workspaces FROM STDIN") as copy:
                copy.write(b"")
        assert getattr(exc.value, "sqlstate", None) in ("0A000", "42501"), exc.value
        pg.rollback()
    finally:
        raw.close()


def test_leaky_temp_function_never_sees_b_rows(app_engine: Engine, world: World) -> None:
    """A cheap side-effecting function in WHERE must run only after the RLS quals
    (security-barrier ordering for non-LEAKPROOF functions)."""
    raw = app_engine.raw_connection()
    notices: list[str] = []
    try:
        pg = raw.driver_connection
        assert pg is not None

        def handler(diag: Any) -> None:
            notices.append(str(diag.message_primary))

        pg.add_notice_handler(handler)
        with pg.cursor() as cur:
            cur.execute(
                "CREATE FUNCTION pg_temp.peek(text) RETURNS boolean LANGUAGE plpgsql COST 0.0001"
                " AS $$ BEGIN RAISE NOTICE 'peek %', $1; RETURN true; END $$"
            )
            cur.execute(
                "SELECT set_config('app.current_user_id', %s, true),"
                " set_config('app.current_org_id', %s, true)",
                (str(world.a.owner_id), str(world.a.org_id)),
            )
            for table, col in (
                ("workspaces", "name"),
                ("organizations", "name"),
                ("users", "subject"),
                ("audit_events", "metadata::text"),
            ):
                cur.execute(f"SELECT count(*) FROM {table} WHERE pg_temp.peek({col})")  # fmt: skip
        pg.rollback()
        pg.remove_notice_handler(handler)
    finally:
        raw.close()
    seen = "\n".join(notices)
    assert world.a.marker in seen  # the probe really ran
    assert world.b.marker not in seen
    assert world.subject(world.b.owner_id) not in seen


def test_app_role_cannot_declare_leakproof(app_engine: Engine) -> None:
    expect_denied(
        app_engine,
        "CREATE FUNCTION pg_temp.lp(text) RETURNS boolean LANGUAGE sql LEAKPROOF AS 'SELECT true'",
        match="leakproof",
    )


def test_error_based_probe_is_armed(admin_engine: Engine, world: World) -> None:
    """Positive control: run by the RLS-bypassing admin, the same probe does hit B's row."""
    with pytest.raises(DBAPIError) as info, admin_engine.connect() as conn:
        conn.execute(
            text(
                "SELECT count(*) FROM workspaces WHERE CASE WHEN name LIKE :mk"
                " THEN 1 / (length(name) - length(name)) = 1 ELSE true END"
            ),
            {"mk": f"{world.b.marker}%"},
        )
    assert sqlstate(info.value) == "22012"


def test_error_based_probe_does_not_fire_on_b_rows(app_engine: Engine, world: World) -> None:
    """x/0 only for B's marker: evaluating the predicate on a B row would raise 22012. (The
    divisor is row-dependent so the planner cannot constant-fold it.)"""
    with app_tx(app_engine, **ctx_a(world)) as conn:
        n = scalar(
            conn,
            "SELECT count(*) FROM workspaces WHERE CASE WHEN name LIKE :mk"
            " THEN 1 / (length(name) - length(name)) = 1 ELSE true END",
            {"mk": f"{world.b.marker}%"},
        )
        assert n >= 1
        n = scalar(
            conn,
            "SELECT count(*) FROM organizations WHERE CASE WHEN id = :b"
            " THEN CAST(name AS int) = 1 ELSE true END",
            {"b": world.b.org_id},
        )
        assert n == 1


def test_temp_table_shadowing_does_not_affect_policies(app_engine: Engine, world: World) -> None:
    """Policies store resolved OIDs: a pg_temp 'organization_memberships' with a forged row
    (A.owner in B) must not make B visible."""
    with app_tx(app_engine, user=world.a.owner_id, org=None) as conn:
        conn.execute(
            text(
                "CREATE TEMP TABLE organization_memberships"
                " (org_id uuid, user_id uuid, role text) ON COMMIT DROP"
            )
        )
        conn.execute(
            text("INSERT INTO organization_memberships VALUES (:b, :u, 'owner')"),
            {"b": world.b.org_id, "u": world.a.owner_id},
        )
        assert scalar(conn, "SELECT count(*) FROM organization_memberships") == 1  # temp one
        orgs = set(conn.execute(text("SELECT id FROM public.organizations")).scalars())
        assert orgs == {world.a.org_id}
        users = set(conn.execute(text("SELECT id FROM public.users")).scalars())
        assert world.b.owner_id not in users


def test_pg_stats_hides_rls_tables(app_engine: Engine, admin_engine: Engine, world: World) -> None:
    """pg_stats would expose most-common values (names, slugs, ids) of every tenant; PostgreSQL
    hides rows for tables where RLS is active for the caller."""
    tables = [*ORG_TABLE_SPECS, "users"]
    stats_sql = "SELECT count(*) FROM pg_stats WHERE schemaname = 'public' AND tablename = ANY(:t)"
    assert admin_count(admin_engine, stats_sql, {"t": tables}) > 0  # statistics do exist
    with app_engine.connect() as conn:
        n = scalar(
            conn,
            "SELECT count(*) FROM pg_stats WHERE schemaname = 'public' AND tablename = ANY(:t)",
            {"t": [*ORG_TABLE_SPECS, "users"]},
        )
        assert n == 0
    expect_denied(app_engine, "SELECT * FROM pg_statistic", match="permission denied")


def test_row_security_off_errors_instead_of_bypassing(app_engine: Engine, world: World) -> None:
    with pytest.raises(DBAPIError) as info, app_tx(app_engine, **ctx_a(world)) as conn:
        conn.execute(text("SET LOCAL row_security = off"))
        conn.execute(text("SELECT count(*) FROM workspaces"))
    assert sqlstate(info.value) == "42501"
    assert "row-level security" in str(info.value.orig)


def test_prepared_plan_does_not_cache_context(app_engine: Engine, world: World) -> None:
    with app_engine.connect() as conn:
        conn.execute(text("PREPARE rls_probe AS SELECT DISTINCT org_id FROM workspaces"))
        conn.commit()
        try:
            for ctx, expected in (
                ({"user": world.a.owner_id, "org": world.a.org_id}, {world.a.org_id}),
                ({"user": world.b.owner_id, "org": world.b.org_id}, {world.b.org_id}),
                ({}, set()),
            ):
                with conn.begin():
                    if ctx:
                        set_ctx(conn, **ctx)
                    for _ in range(7):  # past the generic-plan threshold (5 executions)
                        got = set(conn.execute(text("EXECUTE rls_probe")).scalars())
                        assert got == expected, ctx
        finally:
            conn.execute(text("DEALLOCATE rls_probe"))
            conn.commit()


def test_seed_b_untouched(admin_engine: Engine, world: World) -> None:
    """After every attack above, B is exactly as seeded."""
    b = world.b
    with admin_engine.connect() as conn:
        name, deleted = conn.execute(
            text("SELECT name, deleted_at FROM organizations WHERE id = :o"), {"o": b.org_id}
        ).one()
        assert name.startswith(b.marker) and deleted is None
        roles = dict(
            conn.execute(
                text("SELECT user_id, role FROM organization_memberships WHERE org_id = :o"),
                {"o": b.org_id},
            ).all()
        )
        assert roles[b.owner_id] == "owner" and roles[b.member_id] == "member"
        assert (
            conn.execute(
                text("SELECT revoked_at FROM resource_grants WHERE id = :g"), {"g": b.grant_id}
            ).scalar()
            is None
        )
