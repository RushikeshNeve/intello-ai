"""Tenancy coverage of the M07 memory / research tables (migration 0010), as BOTH roles.

Private rows are visible only with org AND user context of their creator; workspace-visible memories
follow the workspace standing of the context user (approved: members; any state: workspace admins).
"""

from __future__ import annotations

import uuid

import pytest
from rls_m07_world import CASE_IDS, CASES, M07_GRANTS, M07_TABLES, M07World
from rls_world import app_tx, expect_denied, scalar
from sqlalchemy import Engine, text


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_m07_select_without_context_sees_nothing(
    engines: dict[str, Engine], m07: M07World, role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:  # no set_config at all
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_m07_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], m07: M07World, role: str, table: str
) -> None:
    a, b = m07.a, m07.b
    with app_tx(engines[role], org=a.org, user=a.creator) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") >= 1
        assert scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id <> :o", {"o": a.org}) == 0
        col = a.pk_column(table)
        sql = f"SELECT count(*) FROM {table} WHERE {col} = :i"
        assert scalar(conn, sql, {"i": a.pk(table)}) == 1
        assert scalar(conn, sql, {"i": b.pk(table)}) == 0


def test_private_rows_are_invisible_to_other_members_of_the_org(
    engines: dict[str, Engine], m07: M07World
) -> None:
    a = m07.a
    owner = a.tenant.owner_id  # org owner, NOT the creator
    for role in ("app", "worker"):
        with app_tx(engines[role], org=a.org, user=owner) as conn:
            assert scalar(conn, "SELECT count(*) FROM research_sessions") == 0
            assert scalar(conn, "SELECT count(*) FROM research_steps") == 0
            assert scalar(conn, "SELECT count(*) FROM research_reports") == 0
            assert scalar(conn, "SELECT count(*) FROM memories WHERE id = :i", {"i": a.memory}) == 0


def test_workspace_visible_memories_follow_the_workspace_standing(
    engines: dict[str, Engine], m07: M07World
) -> None:
    a = m07.a
    member = a.tenant.member_id  # workspace viewer
    for role in ("app", "worker"):
        with app_tx(engines[role], org=a.org, user=member) as conn:
            seen = set(scalar_list(conn, "SELECT id FROM memories"))
            assert a.ws_approved in seen  # approved: any workspace member
            assert a.ws_suggested not in seen  # a suggestion: only admins (and its owner)
            assert (
                scalar(conn, "SELECT memory_ws_access(:w)", {"w": a.tenant.workspace_id})
                == "member"
            )
        with app_tx(engines[role], org=a.org, user=a.tenant.owner_id) as conn:
            seen = set(scalar_list(conn, "SELECT id FROM memories"))
            assert {a.ws_approved, a.ws_suggested} <= seen
            assert a.memory not in seen
            assert (
                scalar(conn, "SELECT memory_ws_access(:w)", {"w": a.tenant.workspace_id}) == "admin"
            )
    with app_tx(engines["app"], org=a.org, user=m07.b.tenant.owner_id) as conn:
        assert scalar(conn, "SELECT memory_ws_access(:w)", {"w": a.tenant.workspace_id}) is None


def scalar_list(conn: object, sql: str) -> list[uuid.UUID]:
    return list(conn.execute(text(sql)).scalars())  # type: ignore[attr-defined]


def test_a_memory_can_only_be_inserted_as_a_suggestion_of_the_context_user(
    engines: dict[str, Engine], m07: M07World
) -> None:
    a = m07.a
    insert = (
        "INSERT INTO memories (id, org_id, owner_id, kind, statement, state)"
        " VALUES (gen_random_uuid(), :o, :u, 'preference', 'x', :st)"
    )
    with app_tx(engines["app"], org=a.org, user=a.creator) as conn:  # positive control
        conn.execute(text(insert), {"o": a.org, "u": a.creator, "st": "suggested"})
    expect_denied(
        engines["app"], insert, {"o": a.org, "u": a.creator, "st": "approved"},
        match="row-level security", org=a.org, user=a.creator,
    )  # fmt: skip
    expect_denied(
        engines["app"], insert, {"o": a.org, "u": a.tenant.owner_id, "st": "suggested"},
        match="row-level security", org=a.org, user=a.creator,
    )  # fmt: skip
    expect_denied(
        engines["worker"], insert, {"o": a.org, "u": a.creator, "st": "suggested"},
        match="permission denied", org=a.org, user=a.creator,
    )  # fmt: skip


def test_only_the_owner_or_a_workspace_admin_may_update_a_memory(
    engines: dict[str, Engine], m07: M07World
) -> None:
    a = m07.a
    update = "UPDATE memories SET state = 'rejected' WHERE id = :i"
    with app_tx(engines["app"], org=a.org, user=a.creator) as conn:
        assert conn.execute(text(update), {"i": a.memory}).rowcount == 1  # its owner
        assert conn.execute(text(update), {"i": a.ws_suggested}).rowcount == 0  # not visible
    with app_tx(engines["app"], org=a.org, user=a.tenant.owner_id) as conn:
        assert conn.execute(text(update), {"i": a.ws_suggested}).rowcount == 1  # workspace admin
        assert conn.execute(text(update), {"i": a.memory}).rowcount == 0  # someone else's private
    with app_tx(engines["app"], org=a.org, user=a.creator) as conn:
        res = conn.execute(text(update), {"i": m07.b.memory})
        assert res.rowcount == 0  # another org


def test_definer_functions_are_scoped(engines: dict[str, Engine], m07: M07World) -> None:
    a, b = m07.a, m07.b
    expect_denied(engines["app"], "SELECT memory_sweep()", match="permission denied", org=a.org)
    expect_denied(
        engines["app"],
        "SELECT * FROM research_reap_expired(1)",
        match="permission denied",
        org=a.org,
    )
    expect_denied(  # the app role may not invalidate another org's memories
        engines["app"],
        "SELECT memory_invalidate_evidence(:o, ARRAY[gen_random_uuid()], 'evidence_deleted')",
        {"o": b.org},
        states=("42501",),
        match="org mismatch",
        org=a.org,
    )
    with app_tx(engines["app"], org=a.org, user=a.tenant.owner_id) as conn:  # not the creator
        assert (
            conn.execute(text("SELECT * FROM research_cancel(:s)"), {"s": a.session}).first()
            is None
        )
    with app_tx(engines["app"], org=a.org, user=a.creator) as conn:
        row = conn.execute(text("SELECT * FROM research_cancel(:s)"), {"s": a.session}).one()
        assert row.state == "cancelled"
        assert row.cancelled is True


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_m07_grants_match_the_registry(
    admin_engine: Engine, role_names: dict[str, str], migrated: None, role: str, table: str
) -> None:
    grant = M07_GRANTS[role][table]
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


@pytest.mark.parametrize("table", M07_TABLES)
def test_m07_rls_is_enabled_and_forced(admin_engine: Engine, migrated: None, table: str) -> None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
            {"t": table},
        ).one()
    assert tuple(row) == (True, True)
