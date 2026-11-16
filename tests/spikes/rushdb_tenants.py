"""M00.03.01 — RushDB tenant isolation spike.

Acceptance: cross-tenant read and traversal tests deny leakage.

Model under test (ADR-012/028): one RushDB tenant (keyspace, selected by the
`X-RushDB-Tenant` header + `context.scope`) per UKOS organization. Inside an org,
workspace isolation is NOT provided by RushDB and must be a mandatory in-query filter.

Two org tenants A and B are seeded with the same labels, the same index names, the same
property values, the same vector and even one record with the same explicit id. A also
holds an A-only label and an A-only marker string. Every read surface is then probed from
B and must show none of A's records.

Run: uv run --no-project --with pytest --with httpx python -m pytest -q tests/spikes/rushdb_tenants.py
"""

from __future__ import annotations

import json
import uuid

import pytest

from rushdb_support import TOKEN, RushDBError, Tenant, ids, requires_rushdb, tenant_factory, unique_scope  # noqa: F401

pytestmark = requires_rushdb

VEC = "chunk-emb"   # same index names in both tenants on purpose
TXT = "chunk-text"
DIMS = 4
SHARED_ID = str(uuid.uuid4())  # one id that exists in BOTH tenants (ids are tenant-scoped)
A_MARKER = "zebracorn"         # a token that only tenant A's text contains


def _seed_org(t: Tenant, org: str, *, secret: bool) -> dict[str, list[str]]:
    """Two workspaces, documents, chunks with vectors, entities and evidence-like edges."""
    t.create_vector_index(VEC, "Chunk", "embedding", DIMS)
    out: dict[str, list[str]] = {"w1": [], "w2": [], "all": [], "docs": [], "entities": []}
    for ws in ("w1", "w2"):
        doc = t.put("Document", {"org_id": org, "workspace_id": ws, "title": f"Shared title {ws}"})
        out["docs"].append(doc["id"])
        ent = t.put("Entity", {"org_id": org, "workspace_id": ws, "name": "Shared Entity"})
        out["entities"].append(ent["id"])
        for n in range(3):
            text = f"shared phrase quarterly report part {n}"
            if secret:
                text += f" {A_MARKER}"
            rec_id = SHARED_ID if (ws == "w1" and n == 0) else None
            c = t.put("Chunk", {"org_id": org, "workspace_id": ws, "doc_title": f"Shared title {ws}",
                                "text": text, "n": n},
                      id=rec_id,
                      vectors=[{"propertyName": "embedding", "vector": [1, 0.1 * n, 0, 0], "index": VEC}])
            out[ws].append(c["id"])
            out["all"].append(c["id"])
        t.link([doc["id"]], out[ws], "HAS_CHUNK", {"org_id": org, "workspace_id": ws})
        t.link(out[ws][:2], [ent["id"]], "MENTIONS", {"org_id": org, "workspace_id": ws})
    if secret:
        out["secret"] = [t.put("SecretA", {"org_id": org, "workspace_id": "w1", "text": A_MARKER})["id"]]
    t.create_text_index(TXT, "Chunk", "text")
    return out


@pytest.fixture(scope="module")
def orgs():
    a, b = Tenant(unique_scope("orgA")), Tenant(unique_scope("orgB"))
    try:
        seeded_a = _seed_org(a, "org-A", secret=True)
        seeded_b = _seed_org(b, "org-B", secret=False)
        yield a, b, seeded_a, seeded_b
    finally:
        for t in (a, b):
            try:
                t.purge()
            finally:
                t.close()


def _no_a(payload: dict, seeded_a: dict) -> None:
    """Assert a response contains no tenant-A record id (except the deliberately shared id,
    which must then carry B's data) and no A-only marker or org id."""
    blob = json.dumps(payload)
    assert A_MARKER not in blob, f"A-only marker leaked: {blob[:500]}"
    assert "org-A" not in blob, f"org-A data leaked: {blob[:500]}"
    a_only = set(seeded_a["all"]) | set(seeded_a["docs"]) | set(seeded_a["entities"]) | set(seeded_a["secret"])
    a_only.discard(SHARED_ID)
    leaked = [i for i in a_only if i in blob]
    assert not leaked, f"tenant A ids visible from B: {leaked}"


# --------------------------------------------------------------------------------------
# Cross-tenant (org) isolation: everything from B's context
# --------------------------------------------------------------------------------------

def test_sanity_tenant_a_sees_its_own_data(orgs):
    a, _, sa, _ = orgs
    assert len(a.records(["Chunk"])["records"]) == 6
    assert len(a.text(TXT, A_MARKER, 20)["rows"]) == 6
    assert a.get(SHARED_ID)["properties"]["org_id"] == "org-A"


def test_get_by_id(orgs):
    a, b, sa, _ = orgs
    for rid in sa["all"] + sa["docs"] + sa["secret"]:
        if rid == SHARED_ID:
            continue
        with pytest.raises(RushDBError) as e:
            b.get(rid)
        assert e.value.status == 404 and e.value.code == "not_found"
    # Same id in both keyspaces: B gets B's record, never A's.
    rec = b.get(SHARED_ID)
    assert rec["properties"]["org_id"] == "org-B"
    _no_a(rec, sa)
    # $id filter path as well
    r = b.records([], {"$id": {"$in": sa["all"]}})
    assert all(x["properties"]["org_id"] == "org-B" for x in r["records"])
    assert ids(r) in ([SHARED_ID], [])
    _no_a(r, sa)


def test_label_listing_and_catalog(orgs):
    _, b, sa, _ = orgs
    labels = b.describe("labels", query={})["labels"]
    assert "SecretA" not in labels
    assert set(labels) == {"Chunk", "Document", "Entity"}
    schema = b.describe("schema", query={}, detail="catalog")
    _no_a(schema, sa)
    assert "SecretA" not in json.dumps(schema)
    for lbl in ("Chunk", "Document", "Entity", "SecretA"):
        _no_a(b.records([lbl]), sa)
    assert b.records(["SecretA"])["records"] == []
    _no_a(b.records([]), sa)  # every label


def test_property_filter(orgs):
    _, b, sa, _ = orgs
    for pred in ({"org_id": "org-A"}, {"text": {"$contains": A_MARKER}},
                 {"workspace_id": "w1"}, {"doc_title": "Shared title w1"}):
        r = b.records([], pred)
        _no_a(r, sa)
    assert b.records([], {"org_id": "org-A"})["records"] == []


def test_bm25_search(orgs):
    _, b, sa, _ = orgs
    assert b.text(TXT, A_MARKER, 50)["rows"] == []
    r = b.text(TXT, "shared phrase quarterly", 50)
    assert len(r["rows"]) == 6  # B's own six chunks only
    _no_a(r, sa)


def test_vector_search(orgs):
    _, b, sa, _ = orgs
    r = b.vector(VEC, "Chunk", "embedding", [1, 0, 0, 0], 50)
    assert len(r["records"]) == 6
    _no_a(r, sa)
    r = b.vector(VEC, "Chunk", "embedding", [1, 0, 0, 0], 50, mode="ann")
    _no_a(r, sa)


def test_hybrid_search(orgs):
    _, b, sa, _ = orgs
    r = b.hybrid("Chunk", 50, vector={"property": "embedding", "vector": [1, 0, 0, 0], "index": VEC},
                 text={"index": TXT, "query": f"{A_MARKER} shared"})
    assert len(r["records"]) == 6
    _no_a(r, sa)


def test_relationship_traversal(orgs):
    _, b, sa, _ = orgs
    r = b.records(["Chunk"], {"Entity": {"$relation": {"type": "MENTIONS", "direction": "out"},
                                         "name": "Shared Entity"}})
    assert len(r["records"]) == 4
    _no_a(r, sa)
    r = b.records(["Document"], {"Chunk": {"$relation": {"type": "HAS_CHUNK", "direction": "out"},
                                           "text": {"$contains": A_MARKER}}})
    assert r["records"] == []
    # multi-hop: Document -> Chunk -> Entity
    r = b.records(["Document"], {"Chunk": {"Entity": {"name": "Shared Entity"}}})
    assert len(r["records"]) == 2
    _no_a(r, sa)
    rels = b.relationships()
    _no_a(rels, sa)
    assert rels["total"] == 2 * (3 + 2)


def test_count_and_aggregate(orgs):
    _, b, sa, _ = orgs
    r = b.records(["Chunk"], select={"n": {"$count": "*"}})
    assert r["rows"] == [{"n": 6}]
    r = b.records(["Chunk"], select={"ws": "$record.workspace_id", "n": {"$count": "*"}},
                  groupBy=["$record.workspace_id"])
    assert sorted((row["workspace_id"], row["n"]) for row in r["rows"]) == [("w1", 3), ("w2", 3)]
    assert b.statement("sql", "SELECT count(*) AS n FROM chunk")["rows"] == [{"n": 6}]


def test_sql_unknown_label_table(orgs):
    """A label that only exists in A must not even be a table in B."""
    _, b, sa, _ = orgs
    with pytest.raises(RushDBError) as e:
        b.statement("sql", "SELECT * FROM secreta")
    assert e.value.status == 404 and e.value.code == "not_found"


def test_cypher_and_gremlin(orgs):
    _, b, sa, _ = orgs
    r = b.statement("cypher", "MATCH (n) RETURN count(n) AS n")
    assert r["rows"] == [{"n": 6 + 2 + 2}]
    r = b.statement("cypher", "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) RETURN c.org_id AS o, e.name AS e")
    assert len(r["rows"]) == 4 and {row["o"] for row in r["rows"]} == {"org-B"}
    r = b.statement("cypher", "MATCH (n) WHERE n.org_id = 'org-A' RETURN n")
    assert r["rows"] == []
    r = b.statement("cypher", "MATCH (s:SecretA) RETURN s")
    assert r["rows"] == []
    assert b.statement("gremlin", "g.V().has('org_id', 'org-A').count()")["rows"] == [{"count": 0}]
    assert b.statement("gremlin", "g.V().has('org_id', 'org-B').count()")["rows"] == [{"count": 10}]


def test_writes_from_b_cannot_touch_a(orgs):
    a, b, sa, _ = orgs
    # A delete-by-query in B matching A's values deletes nothing in A.
    assert b.delete_records(["SecretA"], {"org_id": "org-A"}) == 0
    # A write to the shared id in B leaves A's record intact.
    b.execute({"kind": "mutate", "mutation": {"kind": "putRecord", "mode": "merge", "id": SHARED_ID,
                                              "properties": {"touched_by": "org-B"}}})
    assert "touched_by" not in a.get(SHARED_ID)["properties"]
    assert len(a.records(["SecretA"])["records"]) == 1
    # Linking to an A-only id from B fails (id unknown in B's keyspace).
    with pytest.raises(RushDBError) as e:
        b.link([b.records(["Document"])["records"][0]["id"]], [sa["secret"][0]], "LEAK")
    assert e.value.code == "not_found"


# --------------------------------------------------------------------------------------
# Fail-closed scope resolution and token model
# --------------------------------------------------------------------------------------

QUERY = {"kind": "query", "query": {"kind": "records", "query": {"labels": ["Chunk"], "limit": 5}}}


def _err(resp):
    try:
        return resp.json().get("error", {}).get("code") if isinstance(resp.json().get("error"), dict) \
            else resp.json().get("error")
    except ValueError:
        return resp.text


def test_missing_scope_is_rejected(orgs):
    a, *_ = orgs
    r = a.raw(QUERY, scope=None, header=None)
    assert r.status_code == 403 and _err(r) == "missing_scope"


def test_header_without_envelope_scope_is_rejected(orgs):
    a, *_ = orgs
    r = a.raw(QUERY, scope=None)
    assert r.status_code == 403 and _err(r) == "missing_scope"


def test_envelope_scope_without_header_is_rejected(orgs):
    a, *_ = orgs
    r = a.raw(QUERY, header=None)
    assert r.status_code in (400, 403) and _err(r) == "missing_scope"


def test_header_envelope_mismatch_is_rejected(orgs):
    a, b, *_ = orgs
    r = a.raw(QUERY, header=b.scope)  # envelope says A, header says B
    assert r.status_code == 403 and _err(r) == "scope_mismatch"


def test_bad_or_missing_token_is_rejected(orgs):
    a, *_ = orgs
    for tok in ("wrong-token", None, TOKEN + "x"):
        r = a.raw(QUERY, token=tok)
        assert r.status_code == 401, (tok, r.status_code, r.text)
        assert "unauthorized" in r.text


def test_unknown_scope_sees_nothing_but_is_auto_registered(orgs):
    """FINDING: an unknown scope is not rejected — the first request registers a fresh,
    empty keyspace. It can never see another tenant's data, but any holder of the operator
    token can create tenants by naming them. The API must derive the scope server-side."""
    t = Tenant(unique_scope("never-seen"))
    try:
        r = t.records([])
        assert r["records"] == []
        assert t.describe("labels", query={})["labels"] == []
        assert t.relationships()["total"] == 0
    finally:
        t.purge()
        t.close()


def test_case_and_byte_exact_scopes(orgs):
    a, *_ = orgs
    twin = Tenant(a.scope.upper())
    try:
        assert twin.records([])["records"] == []
    finally:
        twin.purge()
        twin.close()


def test_purged_scope_reuse_is_empty(tenant_factory):
    t = tenant_factory("reuse")
    t.put("Chunk", {"text": "to be purged"})
    t.purge()
    assert t.records([])["records"] == []


def test_one_operator_token_addresses_every_tenant(orgs):
    """SECURITY INVARIANT: the bootstrap token is valid for every scope. Tenant isolation
    therefore depends on the caller choosing the right scope; only the API may hold the
    token, and it must derive the scope from the authenticated principal (never from
    browser input). Signed per-tenant capability tokens (`--capability-secret`) are the
    engine-level mitigation and are not enabled in this spike."""
    a, b, *_ = orgs
    assert a.token == b.token
    assert a.records(["Chunk"])["total"] == 6 and b.records(["Chunk"])["total"] == 6
    assert a.records(["Chunk"], {"org_id": "org-A"})["total"] == 6
    assert b.records(["Chunk"], {"org_id": "org-B"})["total"] == 6


# --------------------------------------------------------------------------------------
# Inside one tenant: org isolation != workspace isolation
# --------------------------------------------------------------------------------------

WS = {"workspace_id": "w1"}


def test_unfiltered_queries_see_both_workspaces(orgs):
    """Proof that the tenant boundary does not separate workspaces."""
    a, _, sa, _ = orgs
    seen = set(ids(a.records(["Chunk"])))
    assert set(sa["w1"]) <= seen and set(sa["w2"]) <= seen
    assert {r["id"] for r in a.text(TXT, "quarterly", 50)["rows"]} == set(sa["all"])
    assert set(ids(a.vector(VEC, "Chunk", "embedding", [1, 0, 0, 0], 50))) == set(sa["all"])
    assert a.statement("cypher", "MATCH (c:Chunk) RETURN count(c) AS n")["rows"] == [{"n": 6}]


def test_workspace_filter_records_and_property_filter(orgs):
    a, _, sa, _ = orgs
    assert set(ids(a.records(["Chunk"], WS))) == set(sa["w1"])
    assert set(ids(a.records(["Chunk"], {"$and": [WS, {"n": {"$gte": 0}}]}))) == set(sa["w1"])


def test_workspace_filter_get_by_id(orgs):
    """`query.record` has no predicate: the adapter must fetch by id through
    `query.records` + `$id` + workspace filter (or re-check workspace_id app-side)."""
    a, _, sa, _ = orgs
    w2_id = sa["w2"][0]
    assert a.get(w2_id)["properties"]["workspace_id"] == "w2"  # unfiltered get DOES return it
    assert a.records([], {"$and": [WS, {"$id": w2_id}]})["records"] == []
    assert ids(a.records([], {"$and": [WS, {"$id": sa["w1"][0]}]})) == [sa["w1"][0]]


def test_workspace_filter_label_listing_and_count(orgs):
    a, _, sa, _ = orgs
    assert a.records(["Chunk"], WS, select={"n": {"$count": "*"}})["rows"] == [{"n": 3}]
    labels = a.describe("labels", query={"predicate": {"workspace_id": "w2"}})["labels"]
    assert "SecretA" not in labels  # SecretA lives only in w1


def test_workspace_filter_bm25(orgs):
    """`query.text` takes NO predicate: it returns other workspaces' chunks. The filtered
    BM25 path is `query.hybrid` with only a text leg and a predicate."""
    a, _, sa, _ = orgs
    raw = {r["id"] for r in a.text(TXT, "quarterly", 50)["rows"]}
    assert raw & set(sa["w2"]), "query.text cannot be workspace-filtered"
    r = a.hybrid("Chunk", 50, predicate=WS, text={"index": TXT, "query": "quarterly"})
    assert set(ids(r)) == set(sa["w1"])


def test_workspace_filter_vector(orgs):
    a, _, sa, _ = orgs
    for mode in ("exact", "ann", "auto"):
        r = a.vector(VEC, "Chunk", "embedding", [1, 0, 0, 0], 50, predicate=WS, mode=mode)
        assert set(ids(r)) == set(sa["w1"]), mode


def test_workspace_filter_hybrid(orgs):
    a, _, sa, _ = orgs
    r = a.hybrid("Chunk", 50, predicate=WS,
                 vector={"property": "embedding", "vector": [1, 0, 0, 0], "index": VEC},
                 text={"index": TXT, "query": "quarterly"})
    assert set(ids(r)) == set(sa["w1"])


def test_workspace_filter_traversal(orgs):
    a, _, sa, _ = orgs
    # Root and related records both constrained to the workspace.
    r = a.records(["Chunk"], {"$and": [WS, {"Entity": {"$relation": {"type": "MENTIONS", "direction": "out"},
                                                       **WS}}]})
    assert set(ids(r)) == set(sa["w1"][:2])
    # Constraining only the related block is NOT enough: the root must also be filtered.
    r = a.records(["Document"], {"Chunk": {**WS}})
    assert ids(r) == [sa["docs"][0]]
    # Relationship listing filters on relationship properties only -> edges must carry workspace_id.
    rels = a.relationships(predicate=WS)
    assert rels["total"] == 3 + 2
    assert all(x["properties"]["workspace_id"] == "w1" for x in rels["relationships"])


def test_workspace_filter_cypher(orgs):
    a, _, sa, _ = orgs
    r = a.statement("cypher", "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) "
                              "WHERE c.workspace_id = $ws AND e.workspace_id = $ws RETURN c.workspace_id AS w",
                    {"ws": "w1"})
    assert [row["w"] for row in r["rows"]] == ["w1", "w1"]
    r = a.statement("cypher", "MATCH (c:Chunk) WHERE c.workspace_id = $ws RETURN count(c) AS n", {"ws": "w1"})
    assert r["rows"] == [{"n": 3}]
