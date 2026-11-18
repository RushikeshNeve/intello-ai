"""Index evidence: on a seeded ~5,000-document workspace the listing queries use the 0003 indexes.

No new index (and so no migration 0004) is needed: ``(workspace_id, <key>, id)`` serves ascending
and descending order alike because ``id`` always follows the direction of the sort key, and
``document_versions`` has the unique ``(document_id, version_number)`` index for the timeline.
The plans are printed (``pytest -s``) for the handoff. No costs are asserted.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from catalog_support import World, ensure_source
from sqlalchemy import Engine, event, text
from sqlalchemy.engine import Engine as SAEngine

from app.core.db import tenant_session

DOCS = 5000


@pytest.fixture
def big(world: World) -> World:
    source = ensure_source(world.engine, world.org_id, world.ws, world.t.owner.id)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by,"
                " created_at, updated_at)"
                " SELECT gen_random_uuid(), :o, :w, :s, 'doc ' || lpad(g::text, 5, '0'), :u,"
                " now() - (g || ' minutes')::interval, now() - ((g % 977) || ' minutes')::interval"
                " FROM generate_series(1, :n) AS g"
            ),
            {"o": world.org_id, "w": world.ws, "s": source, "u": world.t.owner.id, "n": DOCS},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions (id, org_id, workspace_id, document_id,"
                " version_number, original_filename, content_type, size_bytes, sha256, object_key,"
                " created_by) SELECT gen_random_uuid(), d.org_id, d.workspace_id, d.id, 1,"
                " 'f.pdf', 'application/pdf', 1, :sha,"
                " 'org/' || d.org_id || '/doc/' || d.id || '/' || d.id || '/original', d.created_by"
                " FROM documents d WHERE d.workspace_id = :w"
            ),
            {"w": world.ws, "sha": "a" * 64},
        )
        conn.execute(
            text(
                "UPDATE documents d SET latest_version_id = v.id FROM document_versions v"
                " WHERE v.document_id = d.id AND d.workspace_id = :w"
            ),
            {"w": world.ws},
        )
    with world.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE documents"))
        conn.execute(text("ANALYZE document_versions"))
    return world


class Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, conn: Any, cursor: Any, statement: str, parameters: Any, *rest: Any) -> None:
        self.calls.append((statement, parameters))

    def __enter__(self) -> Capture:
        event.listen(SAEngine, "before_cursor_execute", self)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(SAEngine, "before_cursor_execute", self)

    def select_from(self, table: str) -> tuple[str, Any]:
        found = [
            c
            for c in self.calls
            if c[0].lstrip().upper().startswith("SELECT") and f"FROM {table}" in c[0]
        ]
        assert found, [c[0][:80] for c in self.calls]
        return found[-1]


def explain(world: World, app_engine: Engine, statement: str, parameters: Any) -> str:
    """The plan of ``statement`` as the app role (RLS on), as a JSON text."""
    with tenant_session(user_id=world.t.owner.id, org_id=world.org_id, engine=app_engine) as s:
        rows = s.connection().exec_driver_sql("EXPLAIN (FORMAT JSON) " + statement, parameters)
        plan = rows.scalar_one()
    return json.dumps(plan)


def node_types(plan_json: str) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "Node Type" in node:
                found.append(str(node["Node Type"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(plan_json))
    return found


@pytest.mark.parametrize(
    ("params", "index"),
    [
        ({}, "ix_documents_workspace_id_updated_at"),
        ({"sort": "updated_at", "order": "asc"}, "ix_documents_workspace_id_updated_at"),
        ({"sort": "created_at"}, "ix_documents_workspace_id_created_at"),
        ({"sort": "title"}, "ix_documents_workspace_id_title"),
        ({"sort": "title", "order": "desc"}, "ix_documents_workspace_id_title"),
    ],
)
def test_the_listing_uses_the_workspace_sort_indexes(
    big: World, app_engine: Engine, params: dict[str, str], index: str
) -> None:
    with Capture() as capture:
        res = big.api.get(big.path("/documents"), big.t.owner, params={**params, "limit": 50})
    assert res.status == 200
    assert len(res.body["items"]) == 50
    statement, parameters = capture.select_from("documents")
    plan = explain(big, app_engine, statement, parameters)
    kinds = node_types(plan)
    print(f"\nplan for {params or 'default'}: {kinds}")
    assert index in plan, plan
    assert "Sort" not in kinds, kinds  # the index delivers the order; no sort of 5,000 rows


def test_a_cursor_page_still_uses_the_index(big: World, app_engine: Engine) -> None:
    first = big.api.get(big.path("/documents"), big.t.owner, params={"limit": 50})
    cursor = first.body["next_cursor"]
    with Capture() as capture:
        res = big.api.get(
            big.path("/documents"), big.t.owner, params={"limit": 50, "cursor": cursor}
        )
    assert res.status == 200
    statement, parameters = capture.select_from("documents")
    plan = explain(big, app_engine, statement, parameters)
    assert "ix_documents_workspace_id_updated_at" in plan, plan
    assert "Sort" not in node_types(plan)


def test_the_timeline_uses_the_version_number_index(big: World, app_engine: Engine) -> None:
    doc_id = uuid.UUID(big.api.get(big.path("/documents"), big.t.owner).body["items"][0]["id"])
    with Capture() as capture:
        res = big.api.get(big.doc_path(doc_id, "/versions"), big.t.owner)
    assert res.status == 200
    statement, parameters = capture.select_from("document_versions")
    plan = explain(big, app_engine, statement, parameters)
    print(f"\nplan for timeline: {node_types(plan)}")
    assert "uq_document_versions_document_id_version_number" in plan, plan
