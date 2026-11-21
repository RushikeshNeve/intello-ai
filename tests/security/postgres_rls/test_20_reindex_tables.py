"""Tenancy coverage of ``reindex_jobs`` / ``org_active_spaces`` (migration 0018), BOTH roles."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from rls_world import World, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

REINDEX_TABLES: tuple[str, ...] = ("reindex_jobs", "org_active_spaces")
ROLES = ("app", "worker")
CASES = [(r, t) for r in ROLES for t in REINDEX_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]
_JOB = (
    "INSERT INTO reindex_jobs (id, org_id, from_space, to_space, to_dimensions)"
    " VALUES (:i, :o, 'old', 'new', 8)"
)
_SPACE = "INSERT INTO org_active_spaces (org_id, space_id) VALUES (:o, 'old')"


def _cleanup(admin_engine: Engine, world: World) -> None:
    with admin_engine.begin() as conn:
        for table in REINDEX_TABLES:
            conn.execute(
                text(f"DELETE FROM {table} WHERE org_id IN (:a, :b)"),
                {"a": world.a.org_id, "b": world.b.org_id},
            )


@pytest.fixture
def reindex_rows(admin_engine: Engine, world: World) -> Iterator[dict[str, uuid.UUID]]:
    _cleanup(admin_engine, world)  # the world is shared; rows must not leak between tests
    ids = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with admin_engine.begin() as conn:
        for key, tenant in (("a", world.a), ("b", world.b)):
            conn.execute(text(_JOB), {"i": ids[key], "o": tenant.org_id})
            conn.execute(text(_SPACE), {"o": tenant.org_id})
    yield ids
    _cleanup(admin_engine, world)


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_reindex_select_without_context_sees_nothing(
    engines: dict[str, Engine], reindex_rows: dict[str, uuid.UUID], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_reindex_select_is_confined_to_the_context_org(
    engines: dict[str, Engine],
    world: World,
    reindex_rows: dict[str, uuid.UUID],
    role: str,
    table: str,
) -> None:
    with app_tx(engines[role], org=world.a.org_id, user=world.a.owner_id) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 1
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id <> :o", {"o": world.a.org_id})
            == 0
        )


def test_app_cannot_plan_a_job_for_another_org(
    engines: dict[str, Engine], admin_engine: Engine, world: World
) -> None:
    _cleanup(admin_engine, world)
    ctx = {"org": world.a.org_id, "user": world.a.owner_id}
    with app_tx(engines["app"], **ctx) as conn:
        conn.execute(text(_JOB), {"i": uuid.uuid4(), "o": world.a.org_id})
    _cleanup(admin_engine, world)
    expect_denied(engines["app"], _JOB, {"i": uuid.uuid4(), "o": world.b.org_id}, **ctx)
    expect_denied(engines["app"], "DELETE FROM reindex_jobs", **ctx)
    expect_denied(engines["app"], _SPACE, {"o": world.a.org_id}, **ctx)
