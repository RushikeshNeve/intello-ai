"""Offline retrieval golden set (M04.01.03): recall@5/10, MRR and nDCG@10 over a synthetic corpus,
computed with the in-memory index and the deterministic fake embedder (no model needed).

The baseline lives in ``tests/evaluation/retrieval_golden/baseline.json``; the test fails when a
metric drops below it (beyond a 0.005 rounding tolerance) or anything out of scope is returned.
Refresh after an intentional change: ``UKOS_UPDATE_RETRIEVAL_BASELINE=1 python -m pytest
tests/retrieval/test_golden.py``.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

from app.modules.ai.embeddings import FakeEmbeddingClient
from app.modules.retrieval import gather
from app.modules.retrieval.ranking import postprocess
from tests.retrieval.support import SPEC, index_document, make_scope, new_repo, org_scope

GOLDEN = Path(__file__).resolve().parents[4] / "tests" / "evaluation" / "retrieval_golden"
TOLERANCE = 0.005
ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WORKSPACES = {
    "A": uuid.UUID("00000000-0000-0000-0000-00000000000a"),
    "B": uuid.UUID("00000000-0000-0000-0000-00000000000b"),
}


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    return data


def ndcg_at(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, d in enumerate(ranked[:k]) if d in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return dcg / ideal if ideal else 0.0


def run_golden() -> tuple[dict[str, float], list[str]]:
    corpus, queries = _load("corpus.json"), _load("queries.json")
    emb = FakeEmbeddingClient(SPEC)
    org = org_scope(ORG)
    repo = new_repo(org, emb)
    doc_names: dict[uuid.UUID, str] = {}
    for doc in corpus["documents"]:
        ix = index_document(repo, org, emb, WORKSPACES[doc["ws"]], doc["chunks"])
        doc_names[ix.document_id] = doc["id"]

    recall5: list[float] = []
    recall10: list[float] = []
    rr: list[float] = []
    ndcg: list[float] = []
    leaks: list[str] = []
    for q in queries["queries"]:
        scope = make_scope(ORG, [WORKSPACES[w] for w in q["scope"]])
        gathered = gather(scope, repo, emb, q["query"], candidates=50, max_per_document=3)
        ranked: list[str] = []
        for cand in postprocess(gathered.candidates, 3):
            name = doc_names[cand.hit.document_id]
            if name not in ranked:
                ranked.append(name)
        leaks += [f"{q['id']}:{d}" for d in q.get("forbidden", []) if d in ranked]
        in_scope = {d["id"] for d in corpus["documents"] if d["ws"] in q["scope"]}
        leaks += [f"{q['id']}:{d}" for d in ranked if d not in in_scope]
        relevant = set(q["relevant"])
        if not relevant:
            continue
        recall5.append(len(relevant & set(ranked[:5])) / len(relevant))
        recall10.append(len(relevant & set(ranked[:10])) / len(relevant))
        rr.append(next((1 / (i + 1) for i, d in enumerate(ranked) if d in relevant), 0.0))
        ndcg.append(ndcg_at(ranked, relevant, 10))

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4)

    metrics = {
        "recall@5": mean(recall5),
        "recall@10": mean(recall10),
        "mrr": mean(rr),
        "ndcg@10": mean(ndcg),
    }
    return metrics, leaks


def test_golden_set_shape() -> None:
    corpus, queries = _load("corpus.json"), _load("queries.json")
    assert len(corpus["documents"]) == 20
    assert len(queries["queries"]) == 30


def test_golden_retrieval_does_not_regress_and_never_leaks() -> None:
    metrics, leaks = run_golden()
    assert leaks == []  # permission leakage must be exactly zero
    baseline_path = GOLDEN / "baseline.json"
    if os.environ.get("UKOS_UPDATE_RETRIEVAL_BASELINE") == "1":
        payload = {"queries_scored": 29, "metrics": metrics}
        baseline_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["metrics"]
    for name, floor in baseline.items():
        assert metrics[name] >= floor - TOLERANCE, f"{name} regressed: {metrics[name]} < {floor}"
    assert set(baseline) == set(metrics)
