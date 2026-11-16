"""M00.03.02 — RushDB hybrid retrieval spike.

Acceptance: BM25, vector and filtered fusion return expected fixtures.

Corpus: tests/spikes/fixtures/rushdb_hybrid_corpus.json — 30 original chunks with
hand-made 8-d vectors. Expected rankings are stored in the fixture AND re-derived here
with a reference BM25 / cosine / RRF so a wrong fixture cannot pass silently.

Authorization model exercised (system-boundaries §4.2 "filter before rank"): the API
passes the caller's workspace and the PostgreSQL-published revision ids as an in-query
predicate; RushDB must rank only inside that candidate set.

Run with latency output:
  uv run --no-project --with pytest --with httpx python -m pytest -q -s tests/spikes/rushdb_hybrid.py
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from pathlib import Path

import pytest

from rushdb_support import Tenant, requires_rushdb, timed, unique_scope

pytestmark = requires_rushdb

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "rushdb_hybrid_corpus.json").read_text(encoding="utf-8"))
CHUNKS = CORPUS["chunks"]
EXP = CORPUS["expected"]
Q = CORPUS["queries"]
AUTH = CORPUS["authorized"]
VEC, TXT = "hyb-emb", "hyb-text"
DIMS = CORPUS["dimensions"]
FILTER = {"workspace_id": AUTH["workspace_id"], "revision_id": {"$in": AUTH["published_revision_ids"]}}
UNAUTHORIZED = {c["key"] for c in CHUNKS
                if c["workspace_id"] != AUTH["workspace_id"] or c["revision_id"] not in AUTH["published_revision_ids"]}
UNPUBLISHED = {c["key"] for c in CHUNKS if not c["published"]}
LATENCY: dict[str, tuple[float, float]] = {}


# ---------------------------------------------------------------- reference rankers
def _tok(s: str) -> list[str]:
    return [x for x in re.split(r"[^0-9A-Za-z]+", s.lower()) if x]


def ref_bm25(query: str, k1: float = 1.2, b: float = 0.75) -> dict[str, float]:
    toks = {c["key"]: _tok(c["text"]) for c in CHUNKS}
    n, avg = len(toks), sum(map(len, toks.values())) / len(toks)
    out: dict[str, float] = {}
    for key, ts in toks.items():
        s = 0.0
        for q in set(_tok(query)):
            tf = ts.count(q)
            if tf:
                df = sum(q in v for v in toks.values())
                s += math.log(1 + (n - df + 0.5) / (df + 0.5)) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(ts) / avg))
        if s:
            out[key] = s
    return out


def _cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def ref_vector(qv, pool) -> list[tuple[str, float]]:
    return sorted(((c["key"], _cos(qv, c["vector"])) for c in pool), key=lambda x: -x[1])


def ref_rrf(qv, qt, pool, limit, k=60) -> list[tuple[str, float]]:
    prefetch = 4 * limit
    s = ref_bm25(qt)
    vr = [key for key, _ in ref_vector(qv, pool)][:prefetch]
    tr = sorted((c["key"] for c in pool if c["key"] in s), key=lambda key: -s[key])[:prefetch]
    fused: dict[str, float] = {}
    for leg in (vr, tr):
        for rank, key in enumerate(leg):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused.items(), key=lambda x: -x[1])[:limit]


def authorized(c) -> bool:
    return c["key"] not in UNAUTHORIZED


# ---------------------------------------------------------------- fixture
@pytest.fixture(scope="module")
def corpus():
    t = Tenant(unique_scope("hybrid"))
    try:
        t.create_vector_index(VEC, "Chunk", "embedding", DIMS)
        docs = [{**{k: v for k, v in c.items() if k != "vector"},
                 "$vectors": [{"propertyName": "embedding", "vector": c["vector"], "index": VEC}]} for c in CHUNKS]
        r = t.execute({"kind": "ingest", "ingest": {"kind": "documents", "label": "Chunk", "documents": docs,
                                                    "options": {}}})
        assert r["count"] == len(CHUNKS)
        t.create_text_index(TXT, "Chunk", "text")
        recs = t.records(["Chunk"], limit=100)["records"]
        key_of = {rec["id"]: rec["properties"]["key"] for rec in recs}
        assert len(key_of) == len(CHUNKS)
        # Typed as expected (booleans stay booleans; filters are type-strict).
        assert {type(rec["properties"]["published"]) for rec in recs} == {bool}
        yield t, key_of
    finally:
        try:
            t.purge()
        finally:
            t.close()


def keys(resp, key_of) -> list[str]:
    if "rows" in resp:
        return [key_of[r["id"]] for r in resp["rows"]]
    return [key_of[r["id"]] for r in resp["records"]]


def vec_leg(**kw):
    return {"property": "embedding", "vector": Q["vector"], "index": VEC, **kw}


# ---------------------------------------------------------------- fixture self-check
def test_fixture_expectations_match_reference_rankers():
    s = ref_bm25(Q["bm25_terms"])
    assert sorted(s, key=lambda k: -s[k])[:6] == EXP["bm25_unfiltered_tide_top6"]
    s = ref_bm25(Q["bm25_lighthouse"])
    assert sorted(s, key=lambda k: -s[k])[:3] == EXP["bm25_unfiltered_lighthouse_top3"]
    assert [k for k in sorted(s, key=lambda k: -s[k]) if k not in UNAUTHORIZED] == EXP["bm25_filtered_lighthouse_top4"]
    assert [k for k, _ in ref_vector(Q["vector"], CHUNKS)][:5] == EXP["vector_unfiltered_top5"]
    pool = [c for c in CHUNKS if authorized(c)]
    assert [k for k, _ in ref_vector(Q["vector"], pool)][:6] == EXP["vector_filtered_top6"]
    assert [k for k, _ in ref_rrf(Q["vector"], Q["hybrid_text"], pool, 6)] == EXP["hybrid_filtered_top6"]
    # The corpus is adversarial on purpose: unauthorized chunks outrank authorized ones.
    assert set(EXP["vector_unfiltered_top5"]) <= UNAUTHORIZED
    assert set(EXP["bm25_unfiltered_lighthouse_top3"]) <= UNAUTHORIZED


# ---------------------------------------------------------------- BM25
def test_bm25_unfiltered_order_and_scores(corpus):
    t, key_of = corpus
    r = t.text(TXT, Q["bm25_terms"], 6)
    assert keys(r, key_of) == EXP["bm25_unfiltered_tide_top6"]
    ref = ref_bm25(Q["bm25_terms"])
    for row in r["rows"]:  # tantivy BM25 defaults reproduce exactly (float32)
        assert row["score"] == pytest.approx(ref[key_of[row["id"]]], rel=1e-4)
    r = t.text(TXT, Q["bm25_lighthouse"], 3)
    assert keys(r, key_of) == EXP["bm25_unfiltered_lighthouse_top3"]
    _, med, p95 = timed(t.text, TXT, Q["bm25_terms"], 10)
    LATENCY["bm25 query.text (unfiltered)"] = (med, p95)


def test_bm25_filtered_has_no_post_filter_starvation(corpus):
    """6 unauthorized chunks outrank every authorized one for this query. With the filter
    inside the query the k=4 page is still full and fully authorized."""
    t, key_of = corpus
    r = t.hybrid("Chunk", 4, predicate=FILTER, text={"index": TXT, "query": Q["bm25_lighthouse"]})
    assert keys(r, key_of) == EXP["bm25_filtered_lighthouse_top4"]
    r10 = t.hybrid("Chunk", 10, predicate=FILTER, text={"index": TXT, "query": Q["bm25_lighthouse"]})
    assert keys(r10, key_of) == EXP["bm25_filtered_lighthouse_top4"]  # only 4 authorized matches exist
    assert not set(keys(r10, key_of)) & UNAUTHORIZED
    _, med, p95 = timed(t.hybrid, "Chunk", 4, predicate=FILTER, text={"index": TXT, "query": Q["bm25_lighthouse"]})
    LATENCY["bm25 filtered (hybrid text-only leg)"] = (med, p95)


# ---------------------------------------------------------------- vector
def test_vector_unfiltered_order_and_scores(corpus):
    t, key_of = corpus
    r = t.vector(VEC, "Chunk", "embedding", Q["vector"], 5)
    assert keys(r, key_of) == EXP["vector_unfiltered_top5"]
    ref = dict(ref_vector(Q["vector"], CHUNKS))
    for rec in r["records"]:
        assert rec["score"] == pytest.approx(ref[key_of[rec["id"]]], abs=1e-6)
    _, med, p95 = timed(t.vector, VEC, "Chunk", "embedding", Q["vector"], 10)
    LATENCY["vector exact (unfiltered)"] = (med, p95)


@pytest.mark.parametrize("mode", ["exact", "auto", "ann"])
def test_vector_filtered_before_rank(corpus, mode):
    t, key_of = corpus
    r = t.vector(VEC, "Chunk", "embedding", Q["vector"], 6, predicate=FILTER, mode=mode)
    assert keys(r, key_of) == EXP["vector_filtered_top6"]
    assert len(r["records"]) == 6
    assert r["vectorSearch"]["pool"] == len(CHUNKS) - len(UNAUTHORIZED)
    _, med, p95 = timed(t.vector, VEC, "Chunk", "embedding", Q["vector"], 6, predicate=FILTER, mode=mode)
    LATENCY[f"vector filtered mode={mode}"] = (med, p95)


# ---------------------------------------------------------------- hybrid
def test_hybrid_filtered_rrf_matches_reference(corpus):
    t, key_of = corpus
    r = t.hybrid("Chunk", 6, predicate=FILTER, vector=vec_leg(), text={"index": TXT, "query": Q["hybrid_text"]})
    assert r["fusion"] == "rrf"
    assert keys(r, key_of) == EXP["hybrid_filtered_top6"]
    ref = dict(ref_rrf(Q["vector"], Q["hybrid_text"], [c for c in CHUNKS if authorized(c)], 6))
    for rec in r["records"]:
        assert rec["score"] == pytest.approx(ref[key_of[rec["id"]]], abs=1e-12)
        assert rec["provenance"]  # e.g. ["vector:0", "lexical:2"]
    # Fusion really fuses: the top hybrid hit is neither leg's top hit.
    assert EXP["hybrid_filtered_top6"][0] != EXP["vector_filtered_top6"][0]
    _, med, p95 = timed(t.hybrid, "Chunk", 6, predicate=FILTER, vector=vec_leg(),
                        text={"index": TXT, "query": Q["hybrid_text"]})
    LATENCY["hybrid RRF filtered"] = (med, p95)


def test_hybrid_unfiltered_rrf_matches_reference(corpus):
    t, key_of = corpus
    r = t.hybrid("Chunk", 5, vector=vec_leg(), text={"index": TXT, "query": Q["hybrid_text"]})
    ref = ref_rrf(Q["vector"], Q["hybrid_text"], CHUNKS, 5)
    assert keys(r, key_of) == [k for k, _ in ref]


def test_hybrid_dbsf_filtered_is_authorized(corpus):
    t, key_of = corpus
    r = t.hybrid("Chunk", 6, predicate=FILTER, vector=vec_leg(), text={"index": TXT, "query": Q["hybrid_text"]},
                 fusion="dbsf")
    assert r["fusion"] == "dbsf"
    assert len(r["records"]) == 6 and not set(keys(r, key_of)) & UNAUTHORIZED


def test_unpublished_revision_never_appears(corpus):
    """c05/c06 belong to an unpublished revision of an authorized document and are the
    best possible match for these queries."""
    t, key_of = corpus
    draft_q = "draft revision solar panels lighthouse keeper lamp beam"
    results = [
        t.hybrid("Chunk", 30, predicate=FILTER, text={"index": TXT, "query": draft_q}),
        t.hybrid("Chunk", 30, predicate=FILTER, vector=vec_leg(), text={"index": TXT, "query": draft_q}),
        t.vector(VEC, "Chunk", "embedding", [1, 0, 0, 0, 0, 0, 0.1, 0], 30, predicate=FILTER),
        t.vector(VEC, "Chunk", "embedding", [0.95, 0, 0, 0.1, 0, 0, 0, 0], 30, predicate=FILTER, mode="ann"),
        t.records(["Chunk"], FILTER, limit=100),
    ]
    for r in results:
        got = set(keys(r, key_of))
        assert got and not got & UNPUBLISHED and not got & UNAUTHORIZED
    # ...while the same queries without the filter DO surface them (the filter is what protects).
    assert set(keys(t.text(TXT, draft_q, 3), key_of)) & UNPUBLISHED


# ---------------------------------------------------------------- filtered ANN at scale
def test_filtered_ann_at_scale_no_starvation():
    """Pool above resources.vector-exact-max-pool (5,000) forces the HNSW path.
    6,000 authorized + 6,000 unauthorized records; every unauthorized vector is closer to
    the query than every authorized one. Top-10 must be 10 authorized records."""
    t = Tenant(unique_scope("ann"))
    rnd = random.Random(7)
    try:
        t.create_vector_index("ann-emb", "Chunk", "embedding", DIMS)
        t0 = time.perf_counter()
        for ws, base in (("ws-alpha", 0.3), ("ws-beta", 0.9)):
            for start in range(0, 6000, 1000):
                docs = []
                for i in range(start, start + 1000):
                    v = [base] + [rnd.uniform(0.0, 1.0 - base) for _ in range(DIMS - 1)]
                    docs.append({"workspace_id": ws, "i": i,
                                 "$vectors": [{"propertyName": "embedding", "vector": v, "index": "ann-emb"}]})
                t.execute({"kind": "ingest", "ingest": {"kind": "documents", "label": "Chunk", "documents": docs,
                                                        "options": {}}})
        LATENCY["ingest 12,000 8-d records (12 batches of 1,000), total"] = ((time.perf_counter() - t0) * 1000, 0.0)
        q = [1, 0, 0, 0, 0, 0, 0, 0]
        pred = {"workspace_id": "ws-alpha"}
        exact = t.vector("ann-emb", "Chunk", "embedding", q, 10, predicate=pred, mode="exact")
        def ann_q():
            return t.execute({"kind": "query", "query": {"kind": "vector", "query": {
                "labels": ["Chunk"], "property": "embedding", "index": "ann-emb", "vector": q, "limit": 10,
                "predicate": pred, "mode": "ann", "ef": 64}}})

        # FINDING: even a forced `mode: ann` is answered exactly (fallback `annWarming`)
        # while the HNSW graph builds in the background; poll until the graph is resident.
        t0 = time.perf_counter()
        ann = ann_q()
        while ann["vectorSearch"]["mode"] != "ann":
            assert time.perf_counter() - t0 < 60, f"ANN graph never became resident: {ann['vectorSearch']}"
            time.sleep(0.25)
            ann = ann_q()
        LATENCY["ANN graph warm-up, 12k vectors (first call -> mode=ann)"] = ((time.perf_counter() - t0) * 1000, 0.0)
        print("\nANN vectorSearch:", ann["vectorSearch"], "exact:", exact["vectorSearch"])
        assert ann["vectorSearch"]["mode"] == "ann" and ann["vectorSearch"]["pool"] == 6000
        assert len(ann["records"]) == 10
        assert {r["properties"]["workspace_id"] for r in ann["records"]} == {"ws-alpha"}
        recall = len({r["id"] for r in ann["records"]} & {r["id"] for r in exact["records"]}) / 10
        print(f"ANN recall@10 vs exact: {recall:.2f}")
        assert recall >= 0.8

        last, med, p95 = timed(ann_q)
        LATENCY[f"vector ann filtered 6k/12k (mode={last['vectorSearch'].get('mode')})"] = (med, p95)
        _, med, p95 = timed(t.vector, "ann-emb", "Chunk", "embedding", q, 10, predicate=pred, mode="exact")
        LATENCY["vector exact filtered 6k/12k"] = (med, p95)
        _, med, p95 = timed(t.hybrid, "Chunk", 10, predicate=pred,
                            vector={"property": "embedding", "vector": q, "index": "ann-emb"})
        LATENCY["hybrid vector-only leg filtered 6k/12k"] = (med, p95)
    finally:
        try:
            t.purge()
        finally:
            t.close()


def test_zz_print_latency():
    """Client-observed round-trip latency (localhost, Docker Desktop on Windows)."""
    print("\nRushDB latency (ms): median / p95 over 20 runs")
    for name, (med, p95) in LATENCY.items():
        print(f"  {name:60s} {med:8.2f} / {p95:8.2f}")
