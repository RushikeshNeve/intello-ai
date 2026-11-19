"""Side channels against the M02 + M03 tables, for both roles: leaky functions, error-based
probes, COPY, prepared plans, statistics. (The same attacks on the M01 tables: test_03.)"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from rls_docs_world import CASE_IDS, CASES, SELECT_CASES, DocWorld, ids
from rls_world import app_tx, scalar, sqlstate
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_leaky_function_in_where_never_sees_other_orgs_rows(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """A cheap side-effecting function in WHERE runs only after the RLS qual: it is called with
    org A's values and never with org B's (non-LEAKPROOF ordering)."""
    raw = engines[role].raw_connection()
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
            cur.execute("SELECT set_config('app.current_org_id', %s, true)", (str(docs.a.org),))
            cur.execute(f"SELECT count(*) FROM {table} WHERE pg_temp.peek(org_id::text)")
        pg.rollback()
        pg.remove_notice_handler(handler)
    finally:
        raw.close()
    seen = "\n".join(notices)
    assert str(docs.a.org) in seen, "the probe never ran (not armed)"
    assert str(docs.b.org) not in seen


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_error_based_probe_does_not_fire_on_other_orgs_rows(
    engines: dict[str, Engine], admin_engine: Engine, docs: DocWorld, role: str, table: str
) -> None:
    """``1/0`` only for B's org id: evaluating the predicate on a B row would raise 22012. The
    divisor depends on the row so the planner cannot fold it. The admin (no RLS) is the armed
    positive control."""
    sql = (
        f"SELECT count(*) FROM {table} WHERE CASE WHEN org_id = :b"
        " THEN 1 / (length(org_id::text) - 36) = 1 ELSE true END"
    )
    with pytest.raises(DBAPIError) as info, admin_engine.connect() as conn:
        conn.execute(text(sql), {"b": docs.b.org})
    assert sqlstate(info.value) == "22012"
    with app_tx(engines[role], org=docs.a.org) as conn:
        assert scalar(conn, sql, {"b": docs.b.org}) >= 1


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_copy_to_respects_rls_and_copy_from_is_refused(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    raw = engines[role].raw_connection()
    try:
        pg = raw.driver_connection
        assert pg is not None
        with pg.cursor() as cur:
            cur.execute("SELECT set_config('app.current_org_id', %s, true)", (str(docs.a.org),))
            out = bytearray()
            with cur.copy(f"COPY {table} TO STDOUT") as copy:
                for chunk in copy:
                    out += chunk
            dump = out.decode()
            assert str(docs.a.org) in dump
            assert str(docs.b.org) not in dump
            assert docs.b.title_marker not in dump
        pg.rollback()
        with pg.cursor() as cur, pytest.raises(Exception) as exc:
            with cur.copy(f"COPY {table} FROM STDIN") as copy:
                copy.write(b"")
        assert getattr(exc.value, "sqlstate", None) in ("0A000", "42501"), exc.value
        pg.rollback()
    finally:
        raw.close()


@pytest.mark.parametrize("role", ["app", "worker"])
def test_copy_program_and_file_are_refused(engines: dict[str, Engine], role: str) -> None:
    raw = engines[role].raw_connection()
    try:
        pg = raw.driver_connection
        assert pg is not None
        for statement in (
            "COPY (SELECT 1) TO PROGRAM 'id'",
            "COPY (SELECT 1) TO '/tmp/rls_probe_out'",
            "COPY outbox_events FROM '/etc/passwd'",
        ):
            with pg.cursor() as cur, pytest.raises(Exception) as exc:
                cur.execute(statement)
            assert getattr(exc.value, "sqlstate", None) == "42501", (statement, exc.value)
            pg.rollback()
    finally:
        raw.close()


@pytest.mark.parametrize("role", ["app", "worker"])
def test_prepared_plan_does_not_cache_the_context(
    app_url: URL, worker_url: URL, docs: DocWorld, role: str
) -> None:
    """One server-side prepared statement reused across tenants returns each tenant's rows only
    (the org GUC is read at execution, not baked into a cached plan)."""
    url = (app_url if role == "app" else worker_url).render_as_string(hide_password=False)
    with psycopg.connect(url.replace("postgresql+psycopg", "postgresql")) as conn:
        conn.prepare_threshold = 0  # force server-side preparation from the first execution
        for scope in (docs.a, docs.b, docs.a, docs.b, docs.a):
            conn.execute("SELECT set_config('app.current_org_id', %s, false)", (str(scope.org),))
            rows = conn.execute("SELECT DISTINCT org_id FROM outbox_events").fetchall()
            assert [r[0] for r in rows] == [scope.org]
        conn.execute("SELECT set_config('app.current_org_id', '', false)")
        assert conn.execute("SELECT count(*) FROM outbox_events").fetchone() == (0,)


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_pg_stats_and_estimates_do_not_expose_rows(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """Planner statistics of RLS tables are hidden from non-owners (pg_stats applies the table's
    privileges and policies): no most-common-values of other tenants."""
    with app_tx(engines[role], org=docs.a.org) as conn:
        n = scalar(
            conn,
            "SELECT count(*) FROM pg_stats WHERE schemaname = 'public' AND tablename = :t"
            " AND (most_common_vals IS NOT NULL OR histogram_bounds IS NOT NULL)",
            {"t": table},
        )
        assert n == 0, f"{role} can read statistics of {table}"


# ruff: noqa: PT011, SIM117
