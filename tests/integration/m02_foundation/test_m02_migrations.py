"""Migration 0003 round trip, RLS switches, grants and model/migration parity (M02.02.01)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text

from app.core.models import RLS_TABLES, Base

M02_TABLES = ("sources", "documents", "document_versions", "upload_sessions")
M02_FUNCTIONS = ("ukos_document_versions_immutable", "ukos_upload_sessions_forward_only")
HEAD = "0004_upload_session_title"
PREVIOUS = "0002_rls_hardening"


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _functions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT proname FROM pg_proc WHERE proname = ANY(:n)"),
                {"n": list(M02_FUNCTIONS)},
            ).scalars()
        )


def _index_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n"),
                {"n": name},
            ).scalar()
        )


def test_head_is_0003(alembic_config: Config) -> None:
    assert ScriptDirectory.from_config(alembic_config).get_current_head() == HEAD


def test_upgrade_downgrade_upgrade(
    alembic_config: Config, admin_engine: Engine, migrated: None
) -> None:
    assert set(M02_TABLES) <= _tables(admin_engine)
    assert _functions(admin_engine) == set(M02_FUNCTIONS)
    assert _index_exists(admin_engine, "uq_resource_grants_active")

    # One step back: only M02 objects disappear, M01 stays.
    command.downgrade(alembic_config, PREVIOUS)
    assert _tables(admin_engine) & set(M02_TABLES) == set()
    assert _functions(admin_engine) == set()
    assert not _index_exists(admin_engine, "uq_resource_grants_active")
    assert {"users", "organizations", "workspaces", "resource_grants", "audit_events"} <= _tables(
        admin_engine
    )

    # All the way down, then back to head.
    command.downgrade(alembic_config, "base")
    assert _tables(admin_engine) & set(RLS_TABLES) == set()
    command.upgrade(alembic_config, "head")
    assert set(RLS_TABLES) <= _tables(admin_engine)
    assert _functions(admin_engine) == set(M02_FUNCTIONS)
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == HEAD


def test_downgrade_leaves_no_m02_catalog_objects(
    alembic_config: Config, admin_engine: Engine, migrated: None
) -> None:
    command.downgrade(alembic_config, PREVIOUS)
    try:
        with admin_engine.connect() as conn:
            leftovers = conn.execute(
                text(
                    "SELECT 'policy:' || policyname FROM pg_policies"
                    " WHERE tablename = ANY(:t)"
                    " UNION ALL SELECT 'trigger:' || tgname FROM pg_trigger"
                    " WHERE NOT tgisinternal AND tgname = ANY(:g)"
                ),
                {
                    "t": list(M02_TABLES),
                    "g": ["document_versions_immutable", "upload_sessions_forward_only"],
                },
            ).all()
        assert leftovers == []
    finally:
        command.upgrade(alembic_config, "head")


def test_rls_enabled_and_forced_on_m02_tables(admin_engine: Engine, migrated: None) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
                " WHERE relname = ANY(:t) AND relkind = 'r'"
            ),
            {"t": list(M02_TABLES)},
        ).all()
    assert {r.relname for r in rows} == set(M02_TABLES)
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows), rows


def test_models_registered_as_org_scoped() -> None:
    assert set(M02_TABLES) <= set(RLS_TABLES)
    assert set(Base.metadata.tables) == set(RLS_TABLES)


# Table-level privileges and the column-level UPDATE grants the app role must hold.
TABLE_PRIVS = {
    "sources": {"SELECT", "INSERT"},
    "documents": {"SELECT", "INSERT"},
    "document_versions": {"SELECT", "INSERT"},
    "upload_sessions": {"SELECT", "INSERT"},
}
UPDATE_COLUMNS = {
    "sources": set(),
    "documents": {"title", "latest_version_id", "updated_at", "deleted_at", "deleted_by"},
    "document_versions": {"purged_at"},
    "upload_sessions": {"state", "failure_code", "finalized_at"},
}


@pytest.mark.parametrize("table", M02_TABLES)
def test_app_role_privileges_are_least(admin_engine: Engine, migrated: None, table: str) -> None:
    all_privs = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    with admin_engine.connect() as conn:
        held = {
            p
            for p in all_privs
            if conn.execute(
                text("SELECT has_table_privilege('ukos_app', CAST(:t AS regclass), :p)"),
                {"t": f"public.{table}", "p": p},
            ).scalar_one()
        }
        columns = list(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": table},
            ).scalars()
        )
        updatable = {
            c
            for c in columns
            if conn.execute(
                text("SELECT has_column_privilege('ukos_app', CAST(:t AS regclass), :c, 'UPDATE')"),
                {"t": f"public.{table}", "c": c},
            ).scalar_one()
        }
    assert held == TABLE_PRIVS[table]  # no DELETE, TRUNCATE, REFERENCES, TRIGGER
    assert updatable == UPDATE_COLUMNS[table]


def test_active_grant_index_is_partial_unique(admin_engine: Engine, migrated: None) -> None:
    with admin_engine.connect() as conn:
        definition = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_resource_grants_active'")
        ).scalar_one()
    assert "UNIQUE" in definition
    assert "revoked_at IS NULL" in definition


def test_trigger_functions_are_invoker_with_pinned_search_path(
    admin_engine: Engine, migrated: None
) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT proname, prosecdef, proconfig FROM pg_proc WHERE proname = ANY(:n)"),
            {"n": list(M02_FUNCTIONS)},
        ).all()
        public_exec = conn.execute(
            text(
                "SELECT count(*) FROM pg_proc WHERE proname = ANY(:n)"
                " AND has_function_privilege('ukos_app', oid, 'EXECUTE')"
            ),
            {"n": list(M02_FUNCTIONS)},
        ).scalar_one()
    assert {r.proname for r in rows} == set(M02_FUNCTIONS)
    for row in rows:
        assert row.prosecdef is False
        assert row.proconfig == ["search_path=pg_catalog, public, pg_temp"]
    assert public_exec == 0  # REVOKE ALL ... FROM PUBLIC


def test_models_match_migration(admin_engine: Engine, migrated: None) -> None:
    """Alembic autogenerate against the migrated DB finds no difference."""
    with admin_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []
