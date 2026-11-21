"""Fail closed: with no context (unset, or explicitly empty) the app role sees/changes nothing."""

from __future__ import annotations

import uuid

import pytest
from rls_world import ORG_TABLE_SPECS, World, admin_count, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

from app.core.db import tenant_session
from app.core.models import RLS_TABLES

ALL_TABLES = RLS_TABLES  # users + every org-scoped table


@pytest.mark.parametrize("table", ALL_TABLES)
def test_select_unset_context_sees_nothing(app_engine: Engine, world: World, table: str) -> None:
    with app_tx(app_engine) as conn:  # no set_config at all
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("table", ALL_TABLES)
def test_select_empty_context_sees_nothing(app_engine: Engine, world: World, table: str) -> None:
    with tenant_session(user_id=None, org_id=None, engine=app_engine) as session:
        assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0


@pytest.mark.parametrize("table", ORG_TABLE_SPECS)
def test_insert_without_context_rejected(app_engine: Engine, world: World, table: str) -> None:
    sql, params = ORG_TABLE_SPECS[table].insert(world, world.a)
    expect_denied(app_engine, sql, params, match="row-level security")


def test_users_insert_without_context_rejected(app_engine: Engine, world: World) -> None:
    expect_denied(
        app_engine,
        "INSERT INTO users (id, issuer, subject) VALUES (:id, 'http://issuer.test', :s)",
        {"id": uuid.uuid4(), "s": f"nocontext-{uuid.uuid4()}"},
        match="row-level security",
    )


@pytest.mark.parametrize("table", [t for t, s in ORG_TABLE_SPECS.items() if s.update_set])
def test_update_without_context_affects_nothing(
    app_engine: Engine, admin_engine: Engine, world: World, table: str
) -> None:
    spec = ORG_TABLE_SPECS[table]
    with app_tx(app_engine) as conn:
        result = conn.execute(text(f"UPDATE {table} SET {spec.update_set}"))
        assert result.rowcount == 0


def test_users_update_without_context_affects_nothing(app_engine: Engine, world: World) -> None:
    with app_tx(app_engine) as conn:
        assert conn.execute(text("UPDATE users SET display_name = 'pwned'")).rowcount == 0


@pytest.mark.parametrize("table", ALL_TABLES)
def test_delete_without_context_denied_or_noop(
    app_engine: Engine, admin_engine: Engine, world: World, table: str
) -> None:
    before = admin_count(admin_engine, f"SELECT count(*) FROM {table}")
    spec = ORG_TABLE_SPECS.get(table)
    # eval_sets / eval_cases (migration 0017_evals) and embedding_cache (0013, eviction) hold a
    # DELETE grant but no ORG_TABLE_SPECS entry.
    if (spec is not None and spec.has_delete_grant) or table in (
        "eval_sets",
        "eval_cases",
        "embedding_cache",
        "shadow_candidates",
    ):
        with app_tx(app_engine) as conn:
            assert conn.execute(text(f"DELETE FROM {table}")).rowcount == 0
    else:
        expect_denied(app_engine, f"DELETE FROM {table}", match="permission denied")
    assert admin_count(admin_engine, f"SELECT count(*) FROM {table}") == before


def test_audit_update_without_context_denied(app_engine: Engine, world: World) -> None:
    expect_denied(app_engine, "UPDATE audit_events SET outcome = 'error'", match="permission")


def test_orgless_system_audit_insert_allowed_without_context_documented(
    app_engine: Engine, world: World
) -> None:
    """DC-014 by design: an org-less event with no actor passes the INSERT check even with no
    context (system events). It is write-only: no app-role SELECT can ever read it back."""
    with app_tx(app_engine) as conn:
        conn.execute(
            text(
                "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type,"
                " target_id, outcome) VALUES (:id, NULL, NULL, 'system.x', 'system', 'x',"
                " 'success')"
            ),
            {"id": uuid.uuid4()},
        )
        assert scalar(conn, "SELECT count(*) FROM audit_events") == 0
