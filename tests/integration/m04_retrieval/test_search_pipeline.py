"""M04.01.02-.03 pipeline behaviour through the real API: revalidation, degrade rules, response
shape, validation, rate limiting and audit hygiene."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from catalog_support import World
from ret_support import Rig, doc_ids, search
from sqlalchemy import Engine, event

from app.modules.ai.embeddings import EmbeddingUnavailable
from app.modules.knowledge.ports import KnowledgeStoreUnavailable
from app.modules.retrieval import TokenBucketLimiter

QUERY = "heron migration schedule"


def audit_rows(rig: Rig, world: World) -> list[dict[str, Any]]:
    rows = rig.q(
        "SELECT metadata FROM audit_events WHERE org_id = :o AND event_type = 'search.performed'"
        " ORDER BY occurred_at",
        o=world.org_id,
    )
    return [dict(r[0]) for r in rows]


def test_unpublished_and_superseded_revisions_are_never_returned(rig: Rig, world: World) -> None:
    draft = rig.seed_world(world, title="Draft")
    rig.publish(
        world.org_id, world.ws, draft, ["Heron migration schedule draft."], point=False, flag=False
    )
    flagged_only = rig.seed_world(world, title="FlagOnly")  # flagged in the index, no PG pointer
    rig.publish(
        world.org_id, world.ws, flagged_only, ["Heron migration schedule flag."], point=False
    )
    versioned = rig.seed_world(world, title="Versioned", versions=2)
    old = rig.publish(world.org_id, world.ws, versioned, ["Heron migration schedule old."])
    new = rig.publish(
        world.org_id, world.ws, versioned, ["Heron migration schedule new."], version=2, wait=False
    )
    assert new.revision_id != old.revision_id
    # RushDB lags: the superseded revision is still flagged published in the index.
    found = search(world, world.t.owner, query=QUERY)
    assert doc_ids(found) == [str(versioned.id)]
    (hit,) = found.body["results"]
    assert hit["version_id"] == str(versioned.versions[1])
    assert hit["version_number"] == 2
    assert "new" in hit["snippets"][0]["text"]
    assert "old" not in " ".join(s["text"] for s in hit["snippets"])
    meta = audit_rows(rig, world)[-1]
    assert meta["dropped"] >= 2  # the flag-only and the stale revision were dropped at revalidation


def test_document_deleted_between_retrieval_and_revalidation_is_dropped(
    rig: Rig, world: World
) -> None:
    doc = rig.seed_world(world, title="Vanishing")
    keep = rig.seed_world(world, title="Keeper")
    rig.publish(world.org_id, world.ws, doc, ["Heron migration schedule vanishing."])
    rig.publish(world.org_id, world.ws, keep, ["Heron migration schedule keeper."])

    def delete_it() -> None:
        with rig.admin.begin() as conn:
            conn.exec_driver_sql(
                f"UPDATE documents SET deleted_at = now(), deleted_by = created_by"
                f" WHERE id = '{doc.id}'"
            )

    rig.repo.after["search_keyword"] = delete_it
    found = search(world, world.t.owner, query=QUERY, mode="keyword")
    assert doc_ids(found) == [str(keep.id)]
    assert audit_rows(rig, world)[-1]["dropped"] == 1


def test_revalidation_is_one_query_per_request(rig: Rig, world: World) -> None:
    for i in range(10):
        doc = rig.seed_world(world, title=f"Doc {i}")
        rig.publish(
            world.org_id, world.ws, doc, [f"Heron migration schedule number {i}."], wait=i == 9
        )
    statements: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        found = search(world, world.t.owner, query=QUERY, limit=10)
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    assert len(doc_ids(found)) >= 1
    assert sum("document_publications" in s for s in statements) == 1


def test_embedder_down_degrades_to_keyword(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="D")
    rig.publish(world.org_id, world.ws, doc, ["Heron migration schedule here."])
    rig.embedder.fail_next(EmbeddingUnavailable("down"))
    found = search(world, world.t.owner, query=QUERY)
    assert found.status == 200
    assert found.body["degraded"] == ["embedding_unavailable"]
    assert doc_ids(found) == [str(doc.id)]
    healthy = search(world, world.t.owner, query=QUERY)
    assert healthy.body["degraded"] == []


def test_index_down_is_503_never_empty(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="D")
    rig.publish(world.org_id, world.ws, doc, ["Heron migration schedule here."])
    rig.repo.fail["search_keyword"] = KnowledgeStoreUnavailable("down")
    for mode in ("keyword", "hybrid"):
        res = search(world, world.t.owner, query=QUERY, mode=mode)
        assert res.status == 503
        assert res.body == {"detail": "search_unavailable"}
    del rig.repo.fail["search_keyword"]
    assert search(world, world.t.owner, query=QUERY).status == 200


def test_response_shape_grouping_highlights_and_filters(rig: Rig, world: World) -> None:
    pdf = rig.seed_world(world, title="Big", content_type="application/pdf")
    body = [f"Heron migration schedule part {i} with filler words." for i in range(5)]
    rig.publish(world.org_id, world.ws, pdf, body)
    txt = rig.seed_world(world, title="Small")
    rig.publish(world.org_id, world.ws, txt, ["Another heron note."])
    res = search(world, world.t.owner, query="Heron migration")
    assert res.status == 200
    out = res.body
    assert set(out) == {"query", "results", "degraded", "took_ms", "scope"}
    assert out["query"] == "Heron migration"
    first = out["results"][0]
    assert first["document_id"] == str(pdf.id)
    assert {
        "document_id", "title", "workspace_id", "workspace_slug", "content_type",
        "version_id", "version_number", "score", "snippets",
    } == set(first)  # fmt: skip
    assert 0 < first["score"] <= 1
    assert 1 <= len(first["snippets"]) <= 3  # grouped by document, at most 3 snippets
    for snip in first["snippets"]:
        assert snip["evidence_id"].startswith("e")
        for start, end in snip["highlights"]:
            assert snip["text"][start:end].lower() in {"heron", "migration"}
        assert snip["highlights"]
    only_pdf = search(world, world.t.owner, query="heron", content_types=["application/pdf"])
    assert doc_ids(only_pdf) == [str(pdf.id)]
    capped = search(world, world.t.owner, query="heron", limit=1)
    assert len(capped.body["results"]) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": "ok", "limit": 0},
        {"query": "ok", "limit": 51},
        {"query": "ok", "mode": "fuzzy"},
        {"query": "ok", "workspace_ids": ["not-a-uuid"]},
        {"query": "ok", "extra": 1},
        {},
    ],
)
def test_validation_is_422(rig: Rig, world: World, body: dict[str, Any]) -> None:
    res = world.api.post(f"/v1/orgs/{world.org_id}/search", world.t.owner, body)
    assert res.status == 422
    assert res.body["detail"] == "invalid_request"


def test_rate_limit_429_per_user(rig: Rig, world: World) -> None:
    now = [0.0]
    rig.limiter = TokenBucketLimiter(2, clock=lambda: now[0])
    ok = [search(world, world.t.owner, query="heron").status for _ in range(2)]
    assert ok == [200, 200]
    limited = search(world, world.t.owner, query="heron")
    assert limited.status == 429
    assert limited.body == {"detail": "rate_limited"}
    assert int(limited.raw.headers["retry-after"]) >= 1
    assert search(world, world.t.admin, query="heron").status == 200  # another user is unaffected
    now[0] = 61.0
    assert search(world, world.t.owner, query="heron").status == 200


def test_no_query_text_in_audit_or_logs(
    rig: Rig, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    doc = rig.seed_world(world, title="D")
    rig.publish(world.org_id, world.ws, doc, ["Heron migration schedule here."])
    secret = "xyzzyquerysecret"
    with caplog.at_level(logging.DEBUG):
        res = search(world, world.t.owner, query=f"heron {secret}")
    assert res.status == 200
    assert secret not in caplog.text
    rows = audit_rows(rig, world)
    assert rows
    assert secret not in json.dumps(rows)
    assert set(rows[-1]) == {
        "mode", "workspace_count", "candidates", "evidence", "dropped", "results",
        "degraded", "took_ms",
    }  # fmt: skip
    dumped = rig.q(
        "SELECT row_to_json(a)::text FROM audit_events a WHERE org_id = :o", o=world.org_id
    )
    assert all(secret not in r[0] for r in dumped)


def test_empty_scope_and_no_hits_are_200(rig: Rig, world: World) -> None:
    none = search(world, world.t.owner, query="nothingmatchesthis")
    assert none.status == 200
    assert none.body["results"] == []
    assert none.body["degraded"] == []


def test_openapi_lists_the_new_routes(world: World) -> None:
    spec = world.api.client.get("/openapi.json").json()
    assert "post" in spec["paths"]["/v1/orgs/{org_id}/search"]
    chunk = (
        "/v1/orgs/{org_id}/workspaces/{workspace_id}/documents/{document_id}"
        "/versions/{version_id}/chunks/{chunk_id}"
    )
    assert "get" in spec["paths"][chunk]
    assert {"200", "404", "410", "503"} <= set(spec["paths"][chunk]["get"]["responses"])
    assert "429" in spec["paths"]["/v1/orgs/{org_id}/search"]["post"]["responses"]
