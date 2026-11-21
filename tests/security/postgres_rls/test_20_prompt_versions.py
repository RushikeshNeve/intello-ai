"""Tenancy of ``prompt_versions`` (migration 0016), both roles.

Rows are org-scoped, EXCEPT global defaults (``org_id`` NULL), which every org context reads but
nobody but the seed definer writes. No context sees nothing at all (not even the globals).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from rls_world import World, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

PROMPT_TABLES: tuple[str, ...] = ("prompt_versions",)
SHA = hashlib.sha256(b"x").hexdigest()
INSERT = (
    "INSERT INTO prompt_versions (id, org_id, kind, version, body, sha256, created_by, state)"
    " VALUES (gen_random_uuid(), :o, 'chat_system', 1, 'b', :h, :u, :s)"
)


@pytest.fixture
def seeded(admin_engine: Engine, world: World) -> dict[str, uuid.UUID]:
    ids = {k: uuid.uuid4() for k in ("a", "b", "g")}
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM prompt_versions WHERE body LIKE 'rls-%'"))
        for key, org in (("a", world.a.org_id), ("b", world.b.org_id), ("g", None)):
            conn.execute(
                text(
                    "INSERT INTO prompt_versions (id, org_id, kind, version, body, sha256, state)"
                    " VALUES (:i, :o, 'research_plan', :v, :body, :h, 'draft')"
                ),
                {"i": ids[key], "o": org, "v": 900 + len(key), "body": f"rls-{key}", "h": SHA},
            )
    return ids


@pytest.mark.parametrize("role", ["app", "worker"])
@pytest.mark.parametrize("table", PROMPT_TABLES)
def test_prompt_select_without_context_sees_nothing(
    engines: dict[str, Engine], seeded: dict[str, uuid.UUID], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("role", ["app", "worker"])
@pytest.mark.parametrize("table", PROMPT_TABLES)
def test_prompt_select_is_confined_to_the_context_org(
    engines: dict[str, Engine],
    world: World,
    seeded: dict[str, uuid.UUID],
    role: str,
    table: str,
) -> None:
    with app_tx(engines[role], org=world.a.org_id, user=world.a.owner_id) as conn:
        seen = set(conn.execute(text(f"SELECT id FROM {table} WHERE body LIKE 'rls-%'")).scalars())
    assert seen == {seeded["a"], seeded["g"]}  # own + global, never the other org's
    with app_tx(engines[role], org=world.a.org_id, user=world.a.owner_id) as conn:
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": world.b.org_id})
            == 0
        )


def test_app_writes_are_confined_to_own_org_drafts(
    engines: dict[str, Engine], world: World, seeded: dict[str, uuid.UUID]
) -> None:
    a, b = world.a, world.b
    ctx = {"org": a.org_id, "user": a.owner_id}
    expect_denied(
        engines["app"], INSERT, {"o": b.org_id, "h": SHA, "u": a.owner_id, "s": "draft"}, **ctx
    )
    expect_denied(
        engines["app"], INSERT, {"o": None, "h": SHA, "u": a.owner_id, "s": "draft"}, **ctx
    )
    expect_denied(
        engines["app"], INSERT, {"o": a.org_id, "h": SHA, "u": a.owner_id, "s": "active"}, **ctx
    )
    with app_tx(engines["app"], **ctx) as conn:
        upd = "UPDATE prompt_versions SET state = 'retired' WHERE id = :i"
        assert conn.execute(text(upd), {"i": seeded["b"]}).rowcount == 0  # type: ignore[attr-defined]
        assert conn.execute(text(upd), {"i": seeded["g"]}).rowcount == 0  # type: ignore[attr-defined]
        assert conn.execute(text(upd), {"i": seeded["a"]}).rowcount == 1  # type: ignore[attr-defined]
    expect_denied(
        engines["worker"], INSERT, {"o": a.org_id, "h": SHA, "u": a.owner_id, "s": "draft"}, **ctx
    )
