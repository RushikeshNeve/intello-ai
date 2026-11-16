"""Migration 0001 round-trip, grants and model/migration parity (M01.02.01)."""

from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text

from app.core.models import RLS_TABLES, Base

FUNCTIONS = ("ukos_assert_org_has_owner", "ukos_audit_events_append_only")


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        return set(rows)


def _functions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT proname FROM pg_proc WHERE proname = ANY(:names)"),
            {"names": list(FUNCTIONS)},
        ).scalars()
        return set(rows)


def test_upgrade_downgrade_upgrade(
    alembic_config: Config, admin_engine: Engine, migrated: None
) -> None:
    assert set(RLS_TABLES) <= _tables(admin_engine)
    assert _functions(admin_engine) == set(FUNCTIONS)

    command.downgrade(alembic_config, "base")
    assert _tables(admin_engine) & set(RLS_TABLES) == set()
    assert _functions(admin_engine) == set()

    command.upgrade(alembic_config, "head")
    assert set(RLS_TABLES) <= _tables(admin_engine)
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            "0001_identity_tenancy"
        )


def test_rls_enabled_and_forced_everywhere(admin_engine: Engine, migrated: None) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
                " WHERE relname = ANY(:t) AND relkind = 'r'"
            ),
            {"t": list(RLS_TABLES)},
        ).all()
    assert {r.relname for r in rows} == set(RLS_TABLES)
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows), rows


def test_app_role_has_minimum_privileges(admin_engine: Engine, migrated: None) -> None:
    expected = {
        "users": {"SELECT", "INSERT", "UPDATE"},
        "organizations": {"SELECT", "INSERT", "UPDATE"},
        "organization_memberships": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "workspaces": {"SELECT", "INSERT", "UPDATE"},
        "workspace_memberships": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "resource_grants": {"SELECT", "INSERT", "UPDATE"},
        "audit_events": {"SELECT", "INSERT"},
        "alembic_version": set(),
    }
    all_privs = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    with admin_engine.connect() as conn:
        role = conn.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = 'ukos_app' AND NOT rolbypassrls")
        ).scalar()
        assert role == "ukos_app"
        for table, privs in expected.items():
            granted = {
                p
                for p in all_privs
                if conn.execute(
                    text("SELECT has_table_privilege('ukos_app', :t, :p)"), {"t": table, "p": p}
                ).scalar()
            }
            assert granted == privs, (table, granted)


def test_models_match_migration(admin_engine: Engine, migrated: None) -> None:
    with admin_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []
