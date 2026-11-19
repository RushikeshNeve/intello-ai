"""Tenancy coverage of the M05 graph identity tables (migration 0008), lean.

``graph_entities`` and ``entity_merge_candidates`` are org-scoped: no context sees nothing, a
context sees only its org, neither role may DELETE, and a cross-org INSERT is refused. Registered in
test_16's completeness gate through ``GRAPH_TABLES``.
"""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, app_tx, expect_denied, scalar, sqlstate
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

GRAPH_TABLES: tuple[str, ...] = ("graph_entities", "entity_merge_candidates")
CASES = [(r, t) for r in ("app", "worker") for t in GRAPH_TABLES]
IDS = [f"{r}-{t}" for r, t in CASES]


@pytest.fixture(scope="module")
def graph(admin_engine: Engine, world: World) -> dict[str, tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Per tenant: two entities and one candidate (admin-seeded)."""
    out: dict[str, tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = {}
    with admin_engine.begin() as conn:
        for key, t in (("a", world.a), ("b", world.b)):
            e1, e2 = sorted((uuid.uuid4(), uuid.uuid4()))
            for eid, name in ((e1, "acme corp"), (e2, "acme corporation")):
                conn.execute(
                    text(
                        "INSERT INTO graph_entities (id, org_id, workspace_id, type,"
                        " canonical_name, normalized_name) VALUES (:i, :o, :w, 'organization',"
                        " :n, :n)"
                    ),
                    {"i": eid, "o": t.org_id, "w": t.workspace_id, "n": f"{name} {t.marker}"},
                )
            cand = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO entity_merge_candidates (id, org_id, workspace_id, entity_a,"
                    " entity_b, score) VALUES (:c, :o, :w, :a, :b, 0.9)"
                ),
                {"c": cand, "o": t.org_id, "w": t.workspace_id, "a": e1, "b": e2},
            )
            out[key] = (e1, e2, cand)
    return out


@pytest.mark.parametrize(("role", "table"), CASES, ids=IDS)
def test_graph_select_without_context_sees_nothing(
    engines: dict[str, Engine], graph: dict[str, tuple[uuid.UUID, ...]], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=IDS)
def test_graph_select_is_confined_to_the_context_org(
    engines: dict[str, Engine],
    graph: dict[str, tuple[uuid.UUID, ...]],
    world: World,
    role: str,
    table: str,
) -> None:
    with app_tx(engines[role], user=world.a.member_id, org=world.a.org_id) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") >= 1
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id <> :o", {"o": world.a.org_id})
            == 0
        )


@pytest.mark.parametrize(("role", "table"), CASES, ids=IDS)
def test_graph_tables_have_no_delete_grant(
    engines: dict[str, Engine], world: World, role: str, table: str
) -> None:
    expect_denied(engines[role], f"DELETE FROM {table}", match="permission denied")


def test_worker_cannot_insert_a_foreign_org_entity(
    engines: dict[str, Engine], world: World
) -> None:
    with (
        pytest.raises(DBAPIError) as err,
        app_tx(engines["worker"], user=world.a.member_id, org=world.a.org_id) as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO graph_entities (id, org_id, workspace_id, type, canonical_name,"
                " normalized_name) VALUES (:i, :o, :w, 'person', 'x1', 'x1')"
            ),
            {"i": uuid.uuid4(), "o": world.b.org_id, "w": world.b.workspace_id},
        )
    assert sqlstate(err.value) in {"42501", "23503"}
