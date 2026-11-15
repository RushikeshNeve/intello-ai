"""M00.02.02 — RushDB persistence: restart preserves graph and vector records.

Writes records + relationships + a vector index with bound vectors + a text index into a
fresh tenant, then checks that records, relationship traversal, vector-search and BM25
results are identical after (1) `docker compose restart rushdb` and (2) `down` (volume
kept) + `up`. The fixture tenant is purged at the end; the volume itself is only removed
by the operator's `down -v`.

Run: uv run --no-project --with pytest --with httpx pytest -q tests/spikes/rushdb_persistence.py
"""

from __future__ import annotations

import pytest

from rushdb_support import Tenant, compose, ids, requires_rushdb, unique_scope, wait_ready

pytestmark = requires_rushdb

VEC_INDEX = "persist-emb"
TEXT_INDEX = "persist-text"


def _seed(t: Tenant) -> dict[str, str]:
    t.create_vector_index(VEC_INDEX, "Chunk", "embedding", 4)
    doc = t.put("Document", {"title": "Persistence fixture", "workspace_id": "ws-1"})
    made = {"doc": doc["id"]}
    vecs = {"c1": [1, 0, 0, 0], "c2": [0.8, 0.6, 0, 0], "c3": [0, 1, 0, 0], "c4": [0, 0, 1, 0]}
    texts = {"c1": "durable storage engine", "c2": "write ahead log replay",
             "c3": "volume snapshot restore", "c4": "unrelated gardening note"}
    for key, vec in vecs.items():
        rec = t.put("Chunk", {"key": key, "text": texts[key], "workspace_id": "ws-1"},
                    vectors=[{"propertyName": "embedding", "vector": vec, "index": VEC_INDEX}])
        made[key] = rec["id"]
    t.link([doc["id"]], [made[k] for k in vecs], "HAS_CHUNK")
    ent = t.put("Entity", {"name": "WAL"})
    made["entity"] = ent["id"]
    t.link([made["c2"]], [ent["id"]], "MENTIONS")
    t.create_text_index(TEXT_INDEX, "Chunk", "text")
    return made


def _observe(t: Tenant) -> dict:
    """Everything that must survive a restart, in a comparable form."""
    chunks = t.records(["Chunk"], order={"key": "asc"})
    trav = t.records(["Chunk"], {"Entity": {"$relation": {"type": "MENTIONS", "direction": "out"}, "name": "WAL"}})
    doc_chunks = t.records(["Chunk"], {"Document": {"$relation": {"type": "HAS_CHUNK", "direction": "in"},
                                                    "title": "Persistence fixture"}}, order={"key": "asc"})
    vec = t.vector(VEC_INDEX, "Chunk", "embedding", [1, 0.1, 0, 0], 3)
    text = t.text(TEXT_INDEX, "log replay", 5)
    rels = t.relationships(relationType="HAS_CHUNK")
    return {
        "chunks": [(r["id"], r["properties"]) for r in chunks["records"]],
        "traversal": ids(trav),
        "doc_chunks": ids(doc_chunks),
        "vector": [(r["id"], round(r["score"], 6)) for r in vec["records"]],
        "text": [row["id"] for row in text["rows"]],
        "rels": sorted((r["sourceId"], r["targetId"]) for r in rels["relationships"]),
        "vindex": _indexed_records(t.describe("vectorIndex", name=VEC_INDEX)),
    }


def _indexed_records(stats: dict) -> int | None:
    """`indexedRecords` from a vectorIndexStats response (nested under `stats` or flat)."""
    for node in (stats, stats.get("stats", {}), stats.get("index", {})):
        if isinstance(node, dict) and "indexedRecords" in node:
            return node["indexedRecords"]
    raise AssertionError(f"no indexedRecords in {stats}")


def test_restart_and_recreate_preserve_graph_vector_and_text():
    t = Tenant(unique_scope("persist"))
    try:
        made = _seed(t)
        before = _observe(t)
        # Sanity: the fixture is what we think it is (otherwise "identical" proves nothing).
        assert len(before["chunks"]) == 4
        assert before["traversal"] == [made["c2"]]
        assert len(before["doc_chunks"]) == 4
        assert [i for i, _ in before["vector"]] == [made["c1"], made["c2"], made["c3"]]
        assert before["text"][0] == made["c2"]
        assert len(before["rels"]) == 4
        assert before["vindex"] == 4
        cid_before = compose("ps", "-q", "rushdb").stdout.strip()
        assert cid_before, "rushdb container is not running under this compose project"

        compose("restart", "rushdb")
        wait_ready()
        after_restart = _observe(t)
        assert after_restart == before

        compose("down")  # no -v: the named volume must survive
        compose("up", "-d", "--wait")
        wait_ready()
        cid_after = compose("ps", "-q", "rushdb").stdout.strip()
        assert cid_after and cid_after != cid_before, "down/up must create a new container on the same volume"
        after_recreate = _observe(t)
        assert after_recreate == before
    finally:
        try:
            t.purge()
        finally:
            t.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
