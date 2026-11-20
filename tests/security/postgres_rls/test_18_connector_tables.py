"""Tenancy coverage of the M06 connector tables (migration 0009), as BOTH least-privilege roles."""

from __future__ import annotations

import uuid

import pytest
from rls_connector_world import (
    CASE_IDS,
    CASES,
    CONNECTOR_GRANTS,
    CONNECTOR_TABLES,
    ConnectorWorld,
)
from rls_world import app_tx, expect_denied, scalar
from sqlalchemy import Engine, text


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_connector_select_without_context_sees_nothing(
    engines: dict[str, Engine], connectors: ConnectorWorld, role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:  # no set_config at all
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_connector_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], connectors: ConnectorWorld, role: str, table: str
) -> None:
    a, b = connectors.a, connectors.b
    with app_tx(engines[role], org=a.org) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") >= 1
        assert scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id <> :o", {"o": a.org}) == 0
        sql = f"SELECT count(*) FROM {table} WHERE id = :i"
        assert scalar(conn, sql, {"i": a.pk(table)}) == 1
        assert scalar(conn, sql, {"i": b.pk(table)}) == 0  # a foreign primary key finds nothing


def test_inserts_cannot_cross_orgs_and_sources_cannot_be_updated_across_orgs(
    engines: dict[str, Engine], connectors: ConnectorWorld
) -> None:
    a, b = connectors.a, connectors.b
    insert = (
        "INSERT INTO connector_accounts (id, org_id, provider, label, created_by)"
        " VALUES (gen_random_uuid(), :o, 'github', 'x', :u)"
    )
    with app_tx(engines["app"], org=a.org) as conn:
        conn.execute(text(insert), {"o": a.org, "u": a.tenant.owner_id})  # positive control
    expect_denied(
        engines["app"], insert, {"o": b.org, "u": a.tenant.owner_id}, match="row-level security",
        org=a.org,
    )  # fmt: skip
    for role, column in (("app", "state"), ("worker", "state")):
        with app_tx(engines[role], org=a.org) as conn:  # the foreign source row is invisible
            res = conn.execute(
                text(f"UPDATE sources SET {column} = state WHERE id = :i"), {"i": b.source}
            )
            assert res.rowcount == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_connector_grants_match_the_registry(
    admin_engine: Engine, role_names: dict[str, str], migrated: None, role: str, table: str
) -> None:
    grant = CONNECTOR_GRANTS[role][table]
    with admin_engine.connect() as conn:

        def has(priv: str) -> bool:
            return bool(
                conn.execute(
                    text("SELECT has_table_privilege(:r, CAST(:t AS regclass), :p)"),
                    {"r": role_names[role], "t": f"public.{table}", "p": priv},
                ).scalar_one()
            )

        assert has("SELECT") == grant.select
        assert has("INSERT") == grant.insert
        for forbidden in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not has(forbidden), forbidden
        columns = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).scalars()
        updatable = {
            c
            for c in columns
            if conn.execute(
                text("SELECT has_column_privilege(:r, CAST(:t AS regclass), :c, 'UPDATE')"),
                {"r": role_names[role], "t": f"public.{table}", "c": c},
            ).scalar_one()
        }
    assert updatable == set(grant.update)


@pytest.mark.parametrize("table", CONNECTOR_TABLES)
def test_rls_is_enabled_and_forced(admin_engine: Engine, migrated: None, table: str) -> None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
            {"t": table},
        ).one()
    assert tuple(row) == (True, True)


def test_definer_functions_belong_to_one_role_each(
    engines: dict[str, Engine], connectors: ConnectorWorld
) -> None:
    a = connectors.a
    expect_denied(
        engines["app"], "SELECT connector_enqueue_due_syncs(1)", match="permission denied"
    )
    expect_denied(
        engines["worker"], "SELECT * FROM connector_webhook_lookup(gen_random_uuid())",
        match="permission denied", org=a.org,
    )  # fmt: skip
    with app_tx(engines["app"]) as conn:  # no context: the lookup still finds the sealed secret
        found = conn.execute(
            text("SELECT org_id FROM connector_webhook_lookup(:s)"), {"s": a.source}
        ).one_or_none()
        assert found is not None and found.org_id == a.org
        assert (
            conn.execute(
                text("SELECT * FROM connector_webhook_lookup(:s)"), {"s": uuid.uuid4()}
            ).one_or_none()
            is None
        )
    with app_tx(engines["worker"]) as conn:
        assert scalar(conn, "SELECT connector_enqueue_due_syncs(1)") >= 0
