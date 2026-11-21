"""Tenancy coverage of ``usage_events`` (migration 0012), as BOTH roles: org-keyed, insert-only."""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

USAGE_TABLES: tuple[str, ...] = ("usage_events",)
ROLES = ("app", "worker")
CASES = [(r, t) for r in ROLES for t in USAGE_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]
_INSERT = (
    "INSERT INTO usage_events (id, org_id, kind, provider, model) VALUES (:i, :o, 'chat', 'p', 'm')"
)


@pytest.fixture
def usage_rows(admin_engine: Engine, world: World) -> dict[str, uuid.UUID]:
    ids = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with admin_engine.begin() as conn:
        for key, tenant in (("a", world.a), ("b", world.b)):
            conn.execute(text(_INSERT), {"i": ids[key], "o": tenant.org_id})
    return ids


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_usage_select_without_context_sees_nothing(
    engines: dict[str, Engine], usage_rows: dict[str, uuid.UUID], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_usage_select_is_confined_to_the_context_org(
    engines: dict[str, Engine],
    world: World,
    usage_rows: dict[str, uuid.UUID],
    role: str,
    table: str,
) -> None:
    with app_tx(engines[role], org=world.a.org_id, user=world.a.owner_id) as conn:
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id <> :o", {"o": world.a.org_id})
            == 0
        )
        sql = f"SELECT count(*) FROM {table} WHERE id = :i"
        assert scalar(conn, sql, {"i": usage_rows["a"]}) == 1
        assert scalar(conn, sql, {"i": usage_rows["b"]}) == 0


@pytest.mark.parametrize("role", ROLES)
def test_usage_insert_is_org_bound_and_rows_are_immutable(
    engines: dict[str, Engine], world: World, usage_rows: dict[str, uuid.UUID], role: str
) -> None:
    ctx = {"org": world.a.org_id, "user": world.a.owner_id}
    with app_tx(engines[role], **ctx) as conn:
        conn.execute(text(_INSERT), {"i": uuid.uuid4(), "o": world.a.org_id})
    # Writing into another org is a policy violation; UPDATE / DELETE are not granted at all.
    expect_denied(
        engines[role], _INSERT, {"i": uuid.uuid4(), "o": world.b.org_id}, states=("42501",), **ctx
    )
    expect_denied(engines[role], "DELETE FROM usage_events", **ctx)
    expect_denied(engines[role], "UPDATE usage_events SET model = 'x'", **ctx)
