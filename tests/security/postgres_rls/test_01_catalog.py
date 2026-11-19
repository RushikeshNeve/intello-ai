"""Catalog introspection: RLS switched on everywhere, no trivial policies, least privilege."""

from __future__ import annotations

import pytest
from app.core.models import ORG_SCOPED_TABLES, RLS_TABLES
from rls_world import World
from sqlalchemy import Engine, text

EXPECTED_PRIVILEGES: dict[str, set[str]] = {
    "users": {"SELECT", "INSERT", "UPDATE"},
    "organizations": {"SELECT", "INSERT", "UPDATE"},
    "organization_memberships": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "workspaces": {"SELECT", "INSERT", "UPDATE"},
    "workspace_memberships": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "resource_grants": {"SELECT", "INSERT", "UPDATE"},
    "audit_events": {"SELECT", "INSERT"},
    # M02 (migration 0003): INSERT table-wide, UPDATE only on named columns (column-level grants
    # do not show up in has_table_privilege), never DELETE. Deep coverage is a later task.
    "sources": {"SELECT", "INSERT"},
    "documents": {"SELECT", "INSERT"},
    "document_versions": {"SELECT", "INSERT"},
    "upload_sessions": {"SELECT", "INSERT"},
    # M03 (migration 0005): the API enqueues work (INSERT) and reads status; only the worker role
    # (and the definer functions) update. Deep coverage: tests/integration/m03_foundation.
    "ingestion_jobs": {"SELECT", "INSERT"},
    "ingestion_job_events": {"SELECT", "INSERT"},
    "outbox_events": {"SELECT", "INSERT"},
    "ingestion_checkpoints": {"SELECT"},
    "knowledge_revisions": {"SELECT"},
    "document_publications": {"SELECT"},
}
ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
TRIVIAL = {"true", "(true)", "1 = 1", "(1 = 1)"}


def test_model_lists_match_expected() -> None:
    assert set(ORG_SCOPED_TABLES) <= set(RLS_TABLES)
    assert set(RLS_TABLES) == set(EXPECTED_PRIVILEGES)


@pytest.mark.parametrize("table", RLS_TABLES)
def test_rls_enabled_and_forced(admin_engine: Engine, migrated: None, table: str) -> None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relname = :t AND c.relkind = 'r'"
            ),
            {"t": table},
        ).one()
    assert tuple(row) == (True, True), table


@pytest.mark.parametrize("table", RLS_TABLES)
def test_policies_exist_and_are_not_trivial(
    admin_engine: Engine, migrated: None, table: str
) -> None:
    with admin_engine.connect() as conn:
        policies = conn.execute(
            text(
                "SELECT policyname, permissive, roles, cmd, qual, with_check FROM pg_policies"
                " WHERE schemaname = 'public' AND tablename = :t"
            ),
            {"t": table},
        ).all()
    assert policies, f"{table} has RLS on but no policy (would deny all; check intent)"
    for name, permissive, _roles, cmd, qual, with_check in policies:
        assert permissive == "PERMISSIVE", name
        for expr in (qual, with_check):
            if expr is None:
                continue
            norm = " ".join(expr.lower().split())
            assert norm not in TRIVIAL, f"{name}: trivial expression {expr!r}"
            # Every predicate must be keyed on the transaction-local context.
            assert "current_setting('app.current_" in norm, f"{name}: {expr!r}"
        if cmd in ("SELECT", "DELETE"):
            assert qual is not None, name
        if cmd == "INSERT":
            assert with_check is not None, name
        if cmd in ("UPDATE", "ALL"):
            assert qual is not None and with_check is not None, name


@pytest.mark.parametrize("table", RLS_TABLES)
def test_every_granted_command_has_a_policy(
    admin_engine: Engine, migrated: None, app_role: str, table: str
) -> None:
    """A granted command with no policy would silently deny; one with a policy is intended."""
    with admin_engine.connect() as conn:
        cmds = set(
            conn.execute(
                text("SELECT cmd FROM pg_policies WHERE schemaname = 'public' AND tablename = :t"),
                {"t": table},
            ).scalars()
        )
    covered = {"SELECT", "INSERT", "UPDATE", "DELETE"} if "ALL" in cmds else cmds
    missing = EXPECTED_PRIVILEGES[table] - covered
    # organization_memberships DELETE etc. are covered; audit/users have no DELETE grant.
    assert not missing, f"{table}: granted but no policy for {missing}"


@pytest.mark.parametrize("table", RLS_TABLES)
def test_app_role_privileges_are_exact(
    admin_engine: Engine, migrated: None, app_role: str, table: str
) -> None:
    with admin_engine.connect() as conn:
        held = {
            p
            for p in ALL_PRIVILEGES
            if conn.execute(
                text("SELECT has_table_privilege(:r, CAST(:t AS regclass), :p)"),
                {"r": app_role, "t": f"public.{table}", "p": p},
            ).scalar_one()
        }
    # TRUNCATE bypasses RLS entirely, REFERENCES enables FK existence probes, TRIGGER enables
    # code injection: none may be held.
    assert held == EXPECTED_PRIVILEGES[table], table


def test_no_other_public_relation_is_accessible(
    admin_engine: Engine, migrated: None, app_role: str
) -> None:
    with admin_engine.connect() as conn:
        rels = conn.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','p','f')"
                " AND (has_table_privilege(:r, c.oid, 'SELECT')"
                "   OR has_table_privilege(:r, c.oid, 'INSERT')"
                "   OR has_table_privilege(:r, c.oid, 'UPDATE')"
                "   OR has_table_privilege(:r, c.oid, 'DELETE'))"
            ),
            {"r": app_role},
        ).scalars()
        accessible = set(rels)
    assert accessible == set(RLS_TABLES)  # alembic_version etc. are not reachable


@pytest.mark.parametrize("table", RLS_TABLES)
def test_app_role_does_not_own_tables(
    admin_engine: Engine, migrated: None, app_role: str, table: str
) -> None:
    with admin_engine.connect() as conn:
        owner, member_of_owner = conn.execute(
            text(
                "SELECT pg_get_userbyid(c.relowner), pg_has_role(:r, c.relowner, 'MEMBER')"
                " FROM pg_class c WHERE c.oid = CAST(:t AS regclass)"
            ),
            {"r": app_role, "t": f"public.{table}"},
        ).one()
    assert owner != app_role
    assert member_of_owner is False


def test_app_role_attributes(app_engine: Engine, app_role: str) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,"
                " rolreplication, rolinherit FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
    assert tuple(row)[:6] == (app_role, False, False, False, False, False)


def test_app_role_is_not_member_of_any_role(admin_engine: Engine, app_role: str) -> None:
    """No inherited privileges (pg_read_all_data, pg_write_all_data, the admin, ...)."""
    with admin_engine.connect() as conn:
        granted = conn.execute(
            text(
                "SELECT pg_get_userbyid(m.roleid) FROM pg_auth_members m"
                " WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = :r)"
            ),
            {"r": app_role},
        ).scalars()
        assert list(granted) == []


def test_app_role_cannot_create_in_public_or_database(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT has_schema_privilege('public', 'CREATE'),"
                " has_database_privilege(current_database(), 'CREATE')"
            )
        ).one()
    assert tuple(row) == (False, False)


def test_owner_is_superuser_caveat(admin_engine: Engine, migrated: None) -> None:
    """Documented: the migration owner is the bootstrap superuser, which bypasses RLS no matter
    what FORCE says. FORCE therefore only protects a non-superuser owner (proved by
    ``test_force_rls_restricts_a_non_superuser_owner``). The request path never uses this role.
    """
    with admin_engine.connect() as conn:
        owners = set(
            conn.execute(
                text(
                    "SELECT r.rolsuper FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner"
                    " WHERE c.oid = ANY(CAST(:t AS regclass[]))"
                ),
                {"t": [f"public.{t}" for t in RLS_TABLES]},
            ).scalars()
        )
    assert owners == {True}


def test_force_rls_restricts_a_non_superuser_owner(admin_engine: Engine, world: World) -> None:
    """Hand every table to a throwaway non-superuser owner inside a rolled-back transaction:
    with FORCE on, even the owner sees nothing without context and only its org with context."""
    with admin_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("CREATE ROLE rls_probe_owner NOLOGIN NOSUPERUSER NOBYPASSRLS"))
            for table in RLS_TABLES:
                conn.execute(text(f"ALTER TABLE {table} OWNER TO rls_probe_owner"))
            conn.execute(text("SET LOCAL ROLE rls_probe_owner"))
            for table in RLS_TABLES:
                n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
                assert n == 0, f"owner sees {n} rows of {table} without context"
            conn.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"),
                {"o": str(world.a.org_id)},
            )
            for table in ORG_SCOPED_TABLES:
                col = "id" if table == "organizations" else "org_id"
                other = conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {col} IS DISTINCT FROM :o"),  # noqa: S608
                    {"o": world.a.org_id},
                ).scalar_one()
                assert other == 0, table
        finally:
            trans.rollback()
