"""Hybrid vs hybrid+graph benchmark on relationship questions (M05.03.04). Reported, not gated.

From ``apps/api`` (so ``app.*`` imports):

    python ../../tests/evaluation/graph_benchmark.py

Corpus: the evaluation seed corpus (in-memory index + fake embedder, as ``run_eval``). The graph is
built by a deterministic pattern extractor over the published ``eng-architecture`` chunks (no
model): ``team -[owns]-> service`` and ``service -[uses]-> language``, one verified claim per
sentence with the chunk as evidence. The "model" is the scripted ``ExtractiveAnswerer``. The graph
side goes through the real ``augment_evidence`` (router, traversal, merge, revalidation).

Metrics (per system): ``doc_recall@10``, ``evidence_fact_recall`` (share of expected facts found in
the evidence chunks), ``full_support`` (items with every fact in the evidence),
``answer_fact_rate`` (scripted extractive answer), retrieval latency p50/p95, and the delta.
Writes ``reports/graph_benchmark.json``.
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1] / "apps" / "api"))

import metrics
from adapters import BaselineRetriever, ExtractiveAnswerer, RetrievedChunk
from corpus_build import EvalEnv, build_environment, eval_uuid
from loader import Item, load_dataset

from app.modules.graph.fake_read import FakeChunkSource, FakeGraphRead
from app.modules.graph.merge import augment_evidence
from app.modules.graph.ports_read import GClaim, GEntity, GEvidence
from app.modules.graph.read_settings import GraphReadSettings
from app.modules.graph.router import classify
from app.modules.knowledge.ports import SearchFilters, SearchHit
from app.modules.retrieval.scope import RetrievalScope, resolve_scope

DATASET = HERE / "datasets" / "graph.jsonl"
GOLDEN = HERE / "datasets" / "golden.jsonl"
REPORT = HERE / "reports" / "graph_benchmark.json"
SERVICE = re.compile(r"The (\w+) service is owned by the (\w+) team and is written in (\w+)\.")
DOC = "eng-architecture"


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Answers the statements scope resolution and revalidation issue (published rows only)."""

    def __init__(self, org: uuid.UUID, workspaces: Sequence[uuid.UUID], rows: list[Any]) -> None:
        self.org, self.workspaces, self.rows = org, list(workspaces), rows

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        if "FROM organizations" in sql:
            return _Result([(1,)] if params and params["id"] == self.org else [])
        if "organization_memberships" in sql:
            return _Result(["owner"])
        if "document_publications" in sql:
            wanted = set(params["revs"]) if params else set()
            return _Result([r for r in self.rows if r.revision_id in wanted])
        if "FROM workspaces" in sql:
            return _Result([(w,) for w in self.workspaces])
        raise AssertionError(sql)


class Graph:
    def __init__(self, env: EvalEnv) -> None:
        org = env.org_uuid["org-a"]
        ws = env.workspace_uuid["org-a/engineering"]
        self.org, self.ws = org, ws
        hits: dict[str, SearchHit] = {}
        eng = SearchFilters(frozenset({ws}))
        entities: dict[str, GEntity] = {}
        claims: list[GClaim] = []
        evidence: list[GEvidence] = []

        def entity(kind: str, name: str) -> GEntity:
            key = f"{kind}:{name}"
            if key not in entities:
                entities[key] = GEntity(eval_uuid("ent", key), org, ws, kind, name, "active")
            return entities[key]

        for info in env.chunks.values():
            if info.document_id != DOC or info.state != "published":
                continue
            for service, team, language in SERVICE.findall(info.text):
                if info.chunk_id not in hits:
                    found = env.repo.search_keyword(env.scopes["org-a"], service, eng, limit=20)
                    match = next((h for h in found if str(h.chunk_id) == info.chunk_id), None)
                    assert match is not None, info.chunk_id
                    hits[info.chunk_id] = match
                hit = hits[info.chunk_id]
                svc, tm = entity("project", service), entity("organization", team)
                lang = entity("technology", language)
                for subj, pred, obj, conf in ((tm, "owns", svc, 0.95), (svc, "uses", lang, 0.8)):
                    cid = eval_uuid("claim", subj.canonical_name, pred, obj.canonical_name)
                    claims.append(
                        GClaim(
                            cid,
                            org,
                            ws,
                            subj.id,
                            pred,
                            obj.id,
                            None,
                            conf,
                            "verified",
                            hit.revision_id,
                            True,
                        )
                    )
                    evidence.append(
                        GEvidence(
                            eval_uuid("ev", str(cid)),
                            cid,
                            hit.chunk_id,
                            hit.revision_id,
                            hit.document_id,
                            hit.version_id,
                            "",
                        )
                    )
        self.repo = FakeGraphRead(entities.values(), claims, evidence)
        self.chunks = FakeChunkSource({h.chunk_id: h.text for h in hits.values()})
        self.rows = [
            SimpleNamespace(
                revision_id=h.revision_id,
                document_id=h.document_id,
                version_id=h.version_id,
                workspace_id=h.workspace_id,
                version_number=1,
                content_type="text/markdown",
                title=DOC,
                workspace_slug="engineering",
            )
            for h in {h.revision_id: h for h in hits.values()}.values()
        ]
        self.stats = {"entities": len(entities), "claims": len(claims), "chunks": len(hits)}

    def scope(self) -> RetrievalScope:
        session = FakeSession(self.org, [self.ws], self.rows)
        return resolve_scope(session, org_id=self.org, user_id=uuid.uuid4())  # type: ignore[arg-type]

    def session(self) -> Any:
        return FakeSession(self.org, [self.ws], self.rows)


def _facts(item: Item, chunks: Sequence[RetrievedChunk]) -> list[str]:
    blob = "\n".join(c.text for c in chunks)
    return [f for f in item.expected_facts if metrics.contains(blob, f)]


def _score(item: Item, chunks: Sequence[RetrievedChunk], ms: float) -> dict[str, Any]:
    present = _facts(item, chunks)
    answer = ExtractiveAnswerer().answer(item.question, chunks)
    ranked = metrics.unique_in_order(c.document_id for c in chunks)
    return {
        "docs_recall@10": metrics.recall_at_k(ranked, item.expected_docs, 10),
        "evidence_fact_recall": len(present) / len(item.expected_facts),
        "full_support": len(present) == len(item.expected_facts),
        "answer_fact_rate": sum(1 for f in item.expected_facts if metrics.contains(answer.text, f))
        / len(item.expected_facts),
        "chunks": len(chunks),
        "retrieval_ms": round(ms, 3),
    }


def _aggregate(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ms = [r["retrieval_ms"] for r in rows]
    return {
        "doc_recall@10": round(metrics.mean([r["docs_recall@10"] for r in rows]), 4),
        "evidence_fact_recall": round(metrics.mean([r["evidence_fact_recall"] for r in rows]), 4),
        "full_support_rate": round(
            metrics.mean([1.0 if r["full_support"] else 0.0 for r in rows]), 4
        ),
        "answer_fact_rate": round(metrics.mean([r["answer_fact_rate"] for r in rows]), 4),
        "avg_chunks": round(metrics.mean([float(r["chunks"]) for r in rows]), 2),
        "latency_ms": metrics.latency_summary(ms),
    }


def run() -> dict[str, Any]:
    env = build_environment()
    graph = Graph(env)
    items = load_dataset(DATASET, env.manifest)
    retriever = BaselineRetriever(env)
    settings = GraphReadSettings(GRAPH_MAX_EVIDENCE_EXTRA=8, GRAPH_ROUTER_MODE="auto")
    scope, session = graph.scope(), graph.session()
    per_item: list[dict[str, Any]] = []
    hybrid_rows: list[dict[str, Any]] = []
    graph_rows: list[dict[str, Any]] = []
    for item in items:
        workspaces = list(env.manifest.personas[item.persona].workspaces)
        t0 = time.perf_counter()
        base = retriever.retrieve(item.question, workspaces)
        t1 = time.perf_counter()
        extra = augment_evidence(
            session, scope, item.question, [], graph.repo, graph.chunks, settings=settings
        )
        t2 = time.perf_counter()
        seen = {c.chunk_id for c in base}
        merged = list(base) + [
            RetrievedChunk(str(e.chunk_id), DOC, "org-a/engineering", 1, e.text, e.score)
            for e in extra
            if str(e.chunk_id) not in seen
        ]
        h = _score(item, base, (t1 - t0) * 1000)
        g = _score(item, merged, (t2 - t0) * 1000)
        hybrid_rows.append(h)
        graph_rows.append(g)
        per_item.append(
            {
                "id": item.id,
                "hybrid": h,
                "graph": g,
                "graph_chunks_added": len(merged) - len(base),
                "routed_to_graph": bool(extra),
            }
        )
    golden = load_dataset(GOLDEN, env.manifest)
    routed_golden = sum(1 for g in golden if classify(g.question) == "graph")
    hybrid, with_graph = _aggregate(hybrid_rows), _aggregate(graph_rows)
    report = {
        "schema_version": 1,
        "gated": False,
        "questions": len(items),
        "graph": graph.stats,
        "model": "scripted-extractive (no LLM)",
        "hybrid": hybrid,
        "hybrid_plus_graph": with_graph,
        "delta": {
            "evidence_fact_recall": round(
                with_graph["evidence_fact_recall"] - hybrid["evidence_fact_recall"], 4
            ),
            "full_support_rate": round(
                with_graph["full_support_rate"] - hybrid["full_support_rate"], 4
            ),
            "answer_fact_rate": round(
                with_graph["answer_fact_rate"] - hybrid["answer_fact_rate"], 4
            ),
            "latency_p50_ms": round(
                with_graph["latency_ms"]["p50"] - hybrid["latency_ms"]["p50"], 3
            ),
            "latency_p95_ms": round(
                with_graph["latency_ms"]["p95"] - hybrid["latency_ms"]["p95"], 3
            ),
        },
        "router": {
            "relationship_questions_routed": sum(1 for r in per_item if r["routed_to_graph"]),
            "golden_questions": len(golden),
            "golden_routed_to_graph": routed_golden,
        },
        "items": per_item,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    report = run()
    print(
        json.dumps(
            {k: report[k] for k in ("hybrid", "hybrid_plus_graph", "delta", "router")}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
