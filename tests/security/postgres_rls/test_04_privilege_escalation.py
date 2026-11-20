"""The app role cannot escape RLS: no role switching, no DDL on policies/tables, no catalogs."""

from __future__ import annotations

import pytest
from rls_world import World, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.core.models import RLS_TABLES


@pytest.mark.parametrize(
    "sql",
    [
        "SET ROLE ukos_admin",
        "SET LOCAL ROLE ukos_admin",
        "SET ROLE postgres",
        "SET SESSION AUTHORIZATION ukos_admin",
        "SET ROLE pg_read_all_data",
        "SET ROLE pg_write_all_data",
    ],
)
def test_cannot_switch_role(app_engine: Engine, sql: str) -> None:
    # 42501 permission denied, or 22023 when the role does not exist.
    expect_denied(app_engine, sql, states=("42501", "22023"))


def test_reset_role_is_a_noop(app_engine: Engine) -> None:
    with app_tx(app_engine) as conn:
        conn.execute(text("RESET ROLE"))
        conn.execute(text("RESET SESSION AUTHORIZATION"))
        row = conn.execute(text("SELECT current_user, session_user")).one()
        assert tuple(row) == ("ukos_app", "ukos_app")
        assert scalar(conn, "SELECT count(*) FROM organizations") == 0


@pytest.mark.parametrize("table", RLS_TABLES)
@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE {t} DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY",
        "CREATE POLICY pwn ON {t} FOR SELECT USING (true)",
        "CREATE POLICY pwn ON {t} AS RESTRICTIVE FOR ALL USING (true)",
        "ALTER TABLE {t} OWNER TO ukos_app",
        "ALTER TABLE {t} DISABLE TRIGGER ALL",
        "CREATE TRIGGER pwn BEFORE INSERT ON {t} FOR EACH ROW EXECUTE FUNCTION"
        " ukos_audit_events_append_only()",
        "TRUNCATE {t}",
        "DROP TABLE {t}",
    ],
)
def test_cannot_alter_rls_tables(app_engine: Engine, world: World, table: str, ddl: str) -> None:
    expect_denied(app_engine, ddl.format(t=table))


@pytest.mark.parametrize("table", RLS_TABLES)
def test_self_grant_is_ineffective(app_engine: Engine, table: str) -> None:
    """GRANT without grant option only warns ("no privileges were granted"): verify nothing
    changed rather than expecting an error."""
    with app_tx(app_engine) as conn:
        conn.execute(text(f"GRANT ALL ON {table} TO ukos_app"))
        for priv in ("TRUNCATE", "TRIGGER", "REFERENCES"):
            assert (
                scalar(conn, "SELECT has_table_privilege(CAST(:t AS regclass), :p)",
                       {"t": f"public.{table}", "p": priv})
                is False
            ), priv  # fmt: skip


@pytest.mark.parametrize(
    ("policy", "table"),
    [
        ("workspaces_org", "workspaces"),
        ("users_select", "users"),
        ("audit_events_select", "audit_events"),
        ("organizations_select", "organizations"),
    ],
)
def test_cannot_alter_or_drop_policies(app_engine: Engine, policy: str, table: str) -> None:
    expect_denied(app_engine, f"ALTER POLICY {policy} ON {table} USING (true)")
    expect_denied(app_engine, f"DROP POLICY {policy} ON {table}")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_authid",
        "SELECT rolpassword FROM pg_authid",
        "SELECT * FROM pg_statistic",
        "SELECT pg_read_file('postgresql.conf')",
        "SELECT * FROM pg_ls_dir('.')",
        "COPY (SELECT 1) TO PROGRAM 'id'",
        "COPY (SELECT 1) TO '/tmp/x'",
        "SELECT lo_import('/etc/passwd')",
        "ALTER ROLE ukos_app BYPASSRLS",
        "ALTER ROLE ukos_app SUPERUSER",
        "ALTER ROLE ukos_admin PASSWORD 'x'",
        "CREATE ROLE evil BYPASSRLS",
        "CREATE SCHEMA evil",
        "CREATE TABLE public.evil (id int)",
        "CREATE FUNCTION public.evil() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "CREATE EXTENSION dblink",
        "CREATE EXTENSION postgres_fdw",
        "ALTER DATABASE ukos SET row_security = off",
        "SELECT set_config('session_replication_role', 'replica', true)",
        "GRANT ukos_admin TO ukos_app",
    ],
)
def test_privileged_operations_denied(app_engine: Engine, sql: str) -> None:
    # 42501 insufficient_privilege; 58P01/22023/0A000/42704 are acceptable "it did not run"
    # outcomes for things like a missing extension, but never success.
    expect_denied(app_engine, sql, states=("42501", "0A000", "58P01", "42704", "22023"))


def test_alter_system_denied(app_engine: Engine) -> None:
    """ALTER SYSTEM refuses to run in a transaction block, so try it in autocommit."""
    with (
        pytest.raises(DBAPIError) as info,
        app_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn,
    ):
        conn.execute(text("ALTER SYSTEM SET row_security = off"))
    assert getattr(info.value.orig, "sqlstate", None) == "42501", info.value.orig


def test_temp_view_still_subject_to_rls(app_engine: Engine, world: World) -> None:
    """A view owned by the app role runs with the app role's privileges (and RLS)."""
    with app_tx(app_engine, user=world.a.owner_id, org=world.a.org_id) as conn:
        conn.execute(text("CREATE TEMP VIEW all_ws AS SELECT * FROM public.workspaces"))
        orgs = set(conn.execute(text("SELECT DISTINCT org_id FROM all_ws")).scalars())
        assert orgs == {world.a.org_id}
