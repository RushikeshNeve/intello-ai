"""Tenancy coverage of ``tenant_keys`` (migration 0026, M14.08), as BOTH roles: org-keyed wrapped
data keys; INSERT only under the context org, no UPDATE/DELETE for the runtime roles."""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

TENANT_KEY_TABLES: tuple[str, ...] = ("tenant_keys",)
ROLES = ("app", "worker")
CASES = [(r, t) for r in ROLES for t in TENANT_KEY_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]
_INSERT = (
    "INSERT INTO tenant_keys (id, org_id, kek_id, wrapped_key, state)"
    " VALUES (:i, :o, 'kek1', :w, 'retired')"
)
_WRAPPED = b"\x01" * 60


@pytest.fixture
def key_rows(admin_engine: Engine, world: World) -> dict[str, uuid.UUID]:
    ids = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with admin_engine.begin() as conn:
        for key, tenant in (("a", world.a), ("b", world.b)):
            conn.execute(text(_INSERT), {"i": ids[key], "o": tenant.org_id, "w": _WRAPPED})
    return ids


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_tenant_keys_select_without_context_sees_nothing(
    engines: dict[str, Engine], key_rows: dict[str, uuid.UUID], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_tenant_keys_select_is_confined_to_the_context_org(
    engines: dict[str, Engine],
    world: World,
    key_rows: dict[str, uuid.UUID],
    role: str,
    table: str,
) -> None:
    with app_tx(engines[role], org=world.a.org_id, user=world.a.owner_id) as conn:
        sql = f"SELECT count(*) FROM {table} WHERE id = :i"
        assert scalar(conn, sql, {"i": key_rows["a"]}) == 1
        assert scalar(conn, sql, {"i": key_rows["b"]}) == 0


@pytest.mark.parametrize("role", ROLES)
def test_tenant_keys_insert_into_another_org_and_delete_are_denied(
    engines: dict[str, Engine], world: World, key_rows: dict[str, uuid.UUID], role: str
) -> None:
    ctx = {"org": world.a.org_id, "user": world.a.owner_id}
    params = {"i": uuid.uuid4(), "o": world.b.org_id, "w": _WRAPPED}
    expect_denied(engines[role], _INSERT, params, states=("42501",), **ctx)
    expect_denied(
        engines[role],
        "DELETE FROM tenant_keys WHERE id = :i",
        {"i": key_rows["a"]},
        states=("42501",),
        **ctx,
    )
