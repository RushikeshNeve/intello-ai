"""Tenancy coverage of ``embedding_cache`` (migration 0013), as BOTH roles: org-keyed rows."""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

CACHE_TABLES: tuple[str, ...] = ("embedding_cache",)
ROLES = ("app", "worker")
CASES = [(r, t) for r in ROLES for t in CACHE_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]
_INSERT = (
    "INSERT INTO embedding_cache (org_id, embedding_space_id, text_sha256, vector)"
    " VALUES (:o, 'space', :h, '\\x00')"
)


def _sha() -> str:
    return uuid.uuid4().hex * 2


@pytest.fixture
def cache_rows(admin_engine: Engine, world: World) -> dict[str, str]:
    ids = {"a": _sha(), "b": _sha()}
    with admin_engine.begin() as conn:
        for key, tenant in (("a", world.a), ("b", world.b)):
            conn.execute(text(_INSERT), {"o": tenant.org_id, "h": ids[key]})
    return ids


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_cache_select_without_context_sees_nothing(
    engines: dict[str, Engine], cache_rows: dict[str, str], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_cache_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], world: World, cache_rows: dict[str, str], role: str, table: str
) -> None:
    with app_tx(engines[role], org=world.a.org_id, user=world.a.owner_id) as conn:
        sql = f"SELECT count(*) FROM {table} WHERE text_sha256 = :h"
        assert scalar(conn, sql, {"h": cache_rows["a"]}) == 1
        assert scalar(conn, sql, {"h": cache_rows["b"]}) == 0


@pytest.mark.parametrize("role", ROLES)
def test_cache_insert_into_another_org_is_denied(
    engines: dict[str, Engine], world: World, role: str
) -> None:
    ctx = {"org": world.a.org_id, "user": world.a.owner_id}
    expect_denied(
        engines[role], _INSERT, {"o": world.b.org_id, "h": _sha()}, states=("42501",), **ctx
    )
