"""M00.03.03 — Graph provenance spike.

Acceptance: evidence-linked traversal and deletion tests pass.

Graph model (one org tenant; ADR-014, invariant 7 "evidence lineage"):

    (Claim)-[:SUBJECT]->(Entity)        RushDB relationships cannot themselves point at
    (Claim)-[:OBJECT]->(Entity)         records, so an entity-to-entity fact is REIFIED as
    (Claim)-[:SUPPORTED_BY]->(Evidence) a Claim record. Evidence is one record per
    (Evidence)-[:FROM_CHUNK]->(Chunk)   (claim, chunk) pair and copies the chunk's scope ids
                                        (workspace/document/version/revision) for purge.

Authorization = the chunk's workspace is authorized AND its revision is published
(PostgreSQL decides; the API passes both lists into the query).

Run: uv run --no-project --with pytest --with httpx python -m pytest -q tests/spikes/rushdb_graph.py
"""

from __future__ import annotations

import pytest

from rushdb_support import Tenant, requires_rushdb, unique_scope

pytestmark = requires_rushdb

# chunk key -> (workspace, document, version, revision)
CHUNKS = {
    "k1": ("w1", "doc-1", "doc-1-v1", "doc-1-r1"),
    "k2": ("w1", "doc-1", "doc-1-v1", "doc-1-r1"),
    "k3": ("w1", "doc-1", "doc-1-v2", "doc-1-r2"),
    "k4": ("w2", "doc-2", "doc-2-v1", "doc-2-r1"),
}
ENTITIES = ["Mira Voss", "Tidewater Lab", "Kestrel Engine", "Orrery Guild"]
# claim key -> (subject, predicate, object, [evidence chunk keys])
CLAIMS = {
    "C1": ("Mira Voss", "WORKS_AT", "Tidewater Lab", ["k1"]),            # only doc-1 v1 evidence
    "C2": ("Mira Voss", "BUILT", "Kestrel Engine", ["k1", "k3"]),        # v1 + v2 evidence
    "C3": ("Mira Voss", "MEMBER_OF", "Orrery Guild", ["k4"]),            # only unauthorized workspace
    "C4": ("Mira Voss", "COLLABORATES_WITH", "Tidewater Lab", ["k3", "k4"]),  # mixed authorized/unauthorized
    "C5": ("Kestrel Engine", "LOCATED_AT", "Tidewater Lab", ["k2"]),     # not about Mira
    "C6": ("Tidewater Lab", "EMPLOYS", "Mira Voss", ["k3"]),             # Mira is the OBJECT
}
AUTH_WS = ["w1"]
PUBLISHED = ["doc-1-r1", "doc-1-r2", "doc-2-r1"]


@pytest.fixture
def graph():
    t = Tenant(unique_scope("graph"))
    try:
        ids: dict[str, str] = {}
        for k, (ws, doc, ver, rev) in CHUNKS.items():
            ids[k] = t.put("Chunk", {"key": k, "workspace_id": ws, "document_id": doc, "version_id": ver,
                                     "revision_id": rev, "text": f"chunk {k}"})["id"]
        for name in ENTITIES:
            ids[name] = t.put("Entity", {"name": name})["id"]
        for ck, (s, p, o, ev) in CLAIMS.items():
            ids[ck] = t.put("Claim", {"key": ck, "predicate": p})["id"]
            t.link([ids[ck]], [ids[s]], "SUBJECT")
            t.link([ids[ck]], [ids[o]], "OBJECT")
            for k in ev:
                ws, doc, ver, rev = CHUNKS[k]
                e = t.put("Evidence", {"key": f"{ck}:{k}", "claim": ck, "chunk": k, "workspace_id": ws,
                                       "document_id": doc, "version_id": ver, "revision_id": rev})
                ids[f"{ck}:{k}"] = e["id"]
                t.link([ids[ck]], [e["id"]], "SUPPORTED_BY")
                t.link([e["id"]], [ids[k]], "FROM_CHUNK")
        yield t, ids
    finally:
        try:
            t.purge()
        finally:
            t.close()


def chunk_auth(ws=AUTH_WS, revs=PUBLISHED) -> dict:
    return {"$relation": {"type": "FROM_CHUNK", "direction": "out"},
            "workspace_id": {"$in": ws}, "revision_id": {"$in": revs}}


def claims_about(t: Tenant, entity_id: str, ws=AUTH_WS, revs=PUBLISHED) -> dict[str, list[str]]:
    """ONE native query: claims touching the entity (as subject or object) that have at least
    one authorized evidence, each with ONLY its authorized evidence collected."""
    q = {
        "labels": ["Claim"],
        "predicate": {
            "$and": [
                {"Entity": {"$relation": {"direction": "out"}, "$id": entity_id}},
                {"Evidence": {"$alias": "$ev", "$relation": {"type": "SUPPORTED_BY", "direction": "out"},
                              "Chunk": chunk_auth(ws, revs)}},
            ]
        },
        "select": {"evidence": {"$collect": {"from": "$ev", "select": {"key": "$ev.key"},
                                             "orderBy": {"key": "asc"}}}},
        "limit": 100,
    }
    r = t.execute({"kind": "query", "query": {"kind": "records", "query": q}})
    out = {}
    for rec in r["records"]:
        claim_key = rec["properties"]["key"]
        out[claim_key] = [e["key"].split(":")[1] for e in rec["properties"]["evidence"]]
    return out


def claims_about_cypher(t: Tenant, entity_name: str, ws=AUTH_WS, revs=PUBLISHED) -> dict[str, list[str]]:
    r = t.statement("cypher",
                    "MATCH (e:Entity {name: $name})<-[:SUBJECT|OBJECT]-(c:Claim)-[:SUPPORTED_BY]->(ev:Evidence)"
                    "-[:FROM_CHUNK]->(ch:Chunk) "
                    "WHERE ch.workspace_id IN $ws AND ch.revision_id IN $revs "
                    "RETURN c.key AS claim, ch.key AS chunk ORDER BY claim, chunk",
                    {"name": entity_name, "ws": ws, "revs": revs})
    out: dict[str, list[str]] = {}
    for row in r["rows"]:
        out.setdefault(row["claim"], []).append(row["chunk"])
    return out


EXPECTED_MIRA = {"C1": ["k1"], "C2": ["k1", "k3"], "C4": ["k3"], "C6": ["k3"]}


def test_traversal_returns_only_authorized_evidence_native(graph):
    t, ids = graph
    got = claims_about(t, ids["Mira Voss"])
    # C3 (all evidence unauthorized) excluded; C4 (mixed) keeps only k3; C5 not about Mira.
    assert got == EXPECTED_MIRA


def test_traversal_returns_only_authorized_evidence_cypher(graph):
    t, _ = graph
    assert claims_about_cypher(t, "Mira Voss") == EXPECTED_MIRA


def test_traversal_other_workspace_view(graph):
    t, ids = graph
    assert claims_about(t, ids["Mira Voss"], ws=["w2"]) == {"C3": ["k4"], "C4": ["k4"]}
    assert claims_about(t, ids["Mira Voss"], ws=[]) == {}


def test_unpublished_revision_evidence_excluded(graph):
    """Denial first (invariant 5): unpublishing doc-1 v1 in PostgreSQL removes its
    evidence from traversal before any physical delete happens."""
    t, ids = graph
    revs = [r for r in PUBLISHED if r != "doc-1-r1"]
    assert claims_about(t, ids["Mira Voss"], revs=revs) == {"C2": ["k3"], "C4": ["k3"], "C6": ["k3"]}


def test_other_endpoint_is_returned_with_claim(graph):
    t, ids = graph
    r = t.statement("cypher",
                    "MATCH (s:Entity)<-[:SUBJECT]-(c:Claim)-[:OBJECT]->(o:Entity) "
                    "WHERE c.key IN $keys RETURN c.key AS c, s.name AS s, c.predicate AS p, o.name AS o ORDER BY c",
                    {"keys": list(EXPECTED_MIRA)})
    assert [(x["c"], x["s"], x["p"], x["o"]) for x in r["rows"]] == [
        (k, CLAIMS[k][0], CLAIMS[k][1], CLAIMS[k][2]) for k in sorted(EXPECTED_MIRA)]


def purge_version(t: Tenant, version_id: str) -> dict[str, int]:
    """Documented purge procedure for a deleted document version (app-driven, idempotent):
    1. delete Evidence records copied from the version's chunks
    2. delete the version's Chunk records (their relationships go with them)
    3. delete Claims left with no Evidence
    4. delete Entities left with no Claim
    Every step is a deleteRecords; re-running is a no-op."""
    return {
        "evidence": t.delete_records(["Evidence"], {"version_id": version_id}),
        "chunks": t.delete_records(["Chunk"], {"version_id": version_id}),
        "claims": t.delete_records(["Claim"], {"$not": {"Evidence": {"$relation": {"type": "SUPPORTED_BY"}}}}),
        "entities": t.delete_records(["Entity"], {"$not": {"Claim": {}}}),
    }


def test_deleting_chunks_alone_hides_edges_but_leaves_orphans(graph):
    """Deleting only the chunks already removes v1-only edges from authorized traversal
    (the evidence->chunk hop is gone) but leaves orphan Evidence and Claim records."""
    t, ids = graph
    assert t.delete_records(["Chunk"], {"version_id": "doc-1-v1"}) == 2
    assert claims_about(t, ids["Mira Voss"]) == {"C2": ["k3"], "C4": ["k3"], "C6": ["k3"]}
    assert claims_about_cypher(t, "Mira Voss") == {"C2": ["k3"], "C4": ["k3"], "C6": ["k3"]}
    orphan_ev = t.records(["Evidence"], {"$not": {"Chunk": {}}})
    assert sorted(r["properties"]["key"] for r in orphan_ev["records"]) == ["C1:k1", "C2:k1", "C5:k2"]


def test_version_purge_removes_edges_and_leaves_no_orphans(graph):
    t, ids = graph
    counts = purge_version(t, "doc-1-v1")
    assert counts == {"evidence": 3, "chunks": 2, "claims": 2, "entities": 0}  # C1 and C5 gone
    # Edges evidenced only by v1 disappear; C2 survives with reduced evidence.
    assert claims_about(t, ids["Mira Voss"]) == {"C2": ["k3"], "C4": ["k3"], "C6": ["k3"]}
    assert claims_about_cypher(t, "Mira Voss") == {"C2": ["k3"], "C4": ["k3"], "C6": ["k3"]}
    # No orphans anywhere.
    assert t.records(["Evidence"], {"$not": {"Chunk": {}}})["records"] == []
    assert t.records(["Evidence"], {"$not": {"Claim": {}}})["records"] == []
    assert t.records(["Claim"], {"$not": {"Evidence": {}}})["records"] == []
    assert t.records(["Evidence"], {"version_id": "doc-1-v1"})["records"] == []
    assert t.records(["Chunk"], {"version_id": "doc-1-v1"})["records"] == []
    # Remaining graph is exactly what should remain.
    assert sorted(r["properties"]["key"] for r in t.records(["Claim"])["records"]) == ["C2", "C3", "C4", "C6"]
    assert sorted(r["properties"]["key"] for r in t.records(["Evidence"])["records"]) == \
        ["C2:k3", "C3:k4", "C4:k3", "C4:k4", "C6:k3"]
    rels = t.relationships()
    endpoint_ids = {x["sourceId"] for x in rels["relationships"]} | {x["targetId"] for x in rels["relationships"]}
    assert not endpoint_ids & {ids["k1"], ids["k2"], ids["C1"], ids["C5"], ids["C1:k1"], ids["C2:k1"], ids["C5:k2"]}
    # Idempotent: re-running the purge changes nothing.
    assert purge_version(t, "doc-1-v1") == {"evidence": 0, "chunks": 0, "claims": 0, "entities": 0}


def test_purge_entity_cleanup_when_last_claim_goes(graph):
    """Deleting doc-2 removes C3 (Orrery Guild's only claim) and then the orphan entity."""
    t, ids = graph
    counts = purge_version(t, "doc-2-v1")
    assert counts == {"evidence": 2, "chunks": 1, "claims": 1, "entities": 1}
    assert [r["properties"]["name"] for r in t.records(["Entity"], {"name": "Orrery Guild"})["records"]] == []
    assert claims_about(t, ids["Mira Voss"], ws=["w1", "w2"]) == EXPECTED_MIRA
