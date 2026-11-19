"""Latency of the adapter on a ~5,000-chunk tenant with 768-dimensional random vectors.

Numbers are client round trips on localhost (Docker Desktop on Windows) for the REAL adapter; they
show nothing is pathological, not production capacity (M04 owns the realistic benchmark). Run with
``-s`` to see the table; set ``UKOS_M03_BENCH_OUT`` to also write it as JSON.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
import uuid
from collections.abc import Callable
from itertools import batched

from rdb_support import time_calls

from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
from app.modules.knowledge.ports import OrgScope, SearchFilters
from tests.infrastructure_rushdb.support import make_revision

SPACE_768 = "ebench0000768"
DIMS_768 = 768
CHUNKS = 5000
BATCH = 100


def _unit_vector(rng: random.Random) -> list[float]:
    raw = [rng.gauss(0.0, 1.0) for _ in range(DIMS_768)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def test_latency_on_a_5000_chunk_tenant(
    rushdb_repo: RushDbKnowledgeRepository, scope_factory: Callable[[], OrgScope]
) -> None:
    rng = random.Random(42)  # noqa: S311 - deterministic test data
    vocabulary = [f"w{i:04d}" for i in range(2000)]
    scope = scope_factory()
    ws1, ws2 = uuid.uuid4(), uuid.uuid4()
    rushdb_repo.ensure_indexes(scope, SPACE_768, DIMS_768)

    results: dict[str, dict[str, float]] = {}
    revisions: list[uuid.UUID] = []
    upsert_ms: list[float] = []
    for ws in (ws1, ws2):
        texts = [" ".join(rng.choices(vocabulary, k=60)) for _ in range(CHUNKS // 2)]
        vectors = [_unit_vector(rng) for _ in texts]
        _d, revision, chunks, vecs = make_revision(scope, ws, texts, vectors, space=SPACE_768)
        revisions.append(revision)
        for part, vpart in zip(batched(chunks, BATCH), batched(vecs, BATCH), strict=True):
            started = time.perf_counter()
            rushdb_repo.upsert_chunks(scope, part, vpart)
            upsert_ms.append((time.perf_counter() - started) * 1000)
    upsert_ms.sort()
    results["upsert_batch_100_chunks_768d"] = {
        "n": float(len(upsert_ms)),
        "p50_ms": upsert_ms[len(upsert_ms) // 2],
        "p95_ms": upsert_ms[min(len(upsert_ms) - 1, int(len(upsert_ms) * 0.95))],
    }
    assert sum(rushdb_repo.count_chunks(scope, r) for r in revisions) == CHUNKS

    p50, p95 = time_calls(lambda: [rushdb_repo.set_published(scope, r, True) for r in revisions], 1)
    results["set_published_5000_chunks_total"] = {"n": 1.0, "p50_ms": p50, "p95_ms": p95}

    query_vec = _unit_vector(rng)
    words = " ".join(rng.choices(vocabulary, k=3))
    for label, filters in (
        ("half_tenant", SearchFilters(frozenset({ws1}))),
        ("whole_tenant", SearchFilters(frozenset({ws1, ws2}))),
    ):
        # warm-up (builds the ANN graph / caches), then 30 timed runs each
        rushdb_repo.search_vector(scope, SPACE_768, query_vec, filters, limit=10)
        for name, call in (
            ("keyword", lambda f=filters: rushdb_repo.search_keyword(scope, words, f, limit=10)),
            (
                "vector",
                lambda f=filters: rushdb_repo.search_vector(
                    scope, SPACE_768, query_vec, f, limit=10
                ),
            ),
            (
                "hybrid",
                lambda f=filters: rushdb_repo.search_hybrid(
                    scope, SPACE_768, words, query_vec, f, limit=10
                ),
            ),
        ):
            p50, p95 = time_calls(call, 30)
            results[f"{name}_top10_{label}"] = {"n": 30.0, "p50_ms": p50, "p95_ms": p95}

    table = "\n".join(
        f"{name:<40} n={int(v['n']):<3} p50={v['p50_ms']:8.1f} ms  p95={v['p95_ms']:8.1f} ms"
        for name, v in results.items()
    )
    print("\nM03.03.01 latency (5,000 chunks, 768-d, localhost):\n" + table)
    out = os.environ.get("UKOS_M03_BENCH_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)

    # Sanity bounds only: nothing pathological (generous, to avoid flaky CI).
    for name, v in results.items():
        if not name.startswith("set_published"):
            assert v["p95_ms"] < 5000, f"{name} p95 {v['p95_ms']:.0f} ms is pathological"
