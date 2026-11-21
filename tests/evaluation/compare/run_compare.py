# ruff: noqa: E501
"""Model / embedding / reranker comparison harness (M12.08): config A (baseline) vs B (candidate).

    python tests/evaluation/compare/run_compare.py chat      --a stub:1.0 --b stub:0.5
    python tests/evaluation/compare/run_compare.py embedding --a '{"base_url":"http://localhost:1234/v1",
        "model":"text-embedding-embeddinggemma-300m","dimensions":768}' --b '{"provider":"openai",...}'
    python tests/evaluation/compare/run_compare.py rerank    --a noop --b stub:lexical --fail-on-regression

Config grammar: ``stub:<x>`` (quality / noise / rerank mode), ``noop`` (rerank baseline) or a JSON
object (see ``docs/ops/model-comparison.md``). Output: ``reports/compare/<mode>.{json,md}``.
Exit codes: 0 ok, 2 regression found with ``--fail-on-regression``, 1 usage/config error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

HERE = Path(__file__).resolve().parent
EVAL = HERE.parent
for p in (HERE, EVAL, EVAL / "metrics", EVAL.parents[1] / "apps" / "api"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import rag_metrics as rm
from metrics import hit_at_k, percentile, reciprocal_rank
from stubs import StubChat, StubEmbedder, StubReranker

GOLDEN = EVAL / "retrieval_golden"
OUT_DIR = EVAL.parents[1] / "reports" / "compare"
K = 5
TIE_EPS = 0.005


class Reranker(Protocol):
    """Reorders ``candidates`` ((chunk_id, text) pairs, best first) for ``query``; returns ids."""

    def rerank(self, query: str, candidates: list[tuple[str, str]]) -> list[str]: ...


class NoopReranker:
    simulated_ms = 0.0

    def rerank(self, query: str, candidates: list[tuple[str, str]]) -> list[str]:
        return [cid for cid, _ in candidates]


@dataclass
class Evidence:  # structural EvidenceLike for build_prompt / verify
    evidence_id: str
    text: str
    title: str
    score: float
    document_id: str = ""
    chunk_id: str = ""
    version_id: str = "v1"
    page_start: int | None = None
    page_end: int | None = None
    heading_path: tuple[str, ...] = ()


# ------------------------------------------------------------------------------ data + pricing


def load_dataset(path: Path | None = None) -> tuple[list[dict], list[dict]]:
    """(documents, answerable queries) from the retrieval golden set (or a same-shaped dir)."""
    base = path or GOLDEN
    docs = json.loads((base / "corpus.json").read_text(encoding="utf-8"))["documents"]
    queries = json.loads((base / "queries.json").read_text(encoding="utf-8"))["queries"]
    return docs, [q for q in queries if q.get("relevant")]


def estimate_cost_micros(prices_json: str, provider: str, model: str, t_in: int, t_out: int) -> int:
    """Same table shape as ``USAGE_PRICES_JSON``: ``{"provider/model": {"in": n, "out": n}}``
    (micro-USD per million tokens; ``provider/*`` is the fallback)."""
    try:
        table = json.loads(prices_json or "{}")
        entry = table.get(f"{provider}/{model}") or table.get(f"{provider}/*")
        if not isinstance(entry, dict):
            return 0
        return max(
            0, (t_in * int(entry.get("in", 0)) + t_out * int(entry.get("out", 0))) // 1_000_000
        )
    except (ValueError, TypeError, AttributeError):
        return 0


# ------------------------------------------------------------------------------ config -> objects


def _parse(spec: str) -> dict[str, Any]:
    spec = spec.strip()
    if spec.startswith("{"):
        return json.loads(spec)
    kind, _, arg = spec.partition(":")
    if kind in ("stub", "noop"):
        return {"stub": True, "arg": arg, "provider": "stub", "model": spec}
    raise ValueError(f"bad config {spec!r}: use stub:<x>, noop or a JSON object")


def _key(cfg: dict) -> str | None:
    env = cfg.get("api_key_env")
    return os.environ.get(env) if env else cfg.get("api_key")


def make_chat(cfg: dict) -> Any:
    if cfg.get("stub"):
        return StubChat(float(cfg["arg"] or 1.0))
    from app.modules.ai.models.local import LocalChatModel
    from app.modules.ai.models.openai_adapter import OpenAIChatModel
    from app.modules.ai.models.types import ModelSpec

    hosted = cfg.get("provider", "local") == "openai"
    spec = ModelSpec(
        provider="openai" if hosted else "local",
        model_id=cfg["model"],
        context_tokens=int(cfg.get("context_tokens", 8192)),
        max_output_tokens=int(cfg.get("max_output_tokens", 512)),
    )
    cls = OpenAIChatModel if hosted else LocalChatModel
    return cls(
        spec,
        base_url=cfg["base_url"],
        api_key=_key(cfg),
        allow_external=hosted or bool(cfg.get("allow_external")),
    )


def make_embedder(cfg: dict) -> Any:
    if cfg.get("stub"):
        return StubEmbedder(noise=float(cfg["arg"] or 0.0))
    from app.modules.ai.embeddings.client import EmbeddingClient
    from app.modules.ai.embeddings.registry import EmbeddingModelSpec

    spec = EmbeddingModelSpec(
        model_id=cfg["model"],
        dimensions=int(cfg["dimensions"]),
        query_prefix=cfg.get("query_prefix", ""),
        document_prefix=cfg.get("document_prefix", ""),
        max_input_chars=int(cfg.get("max_input_chars", 8000)),
        spec_version=int(cfg.get("spec_version", 1)),
    )
    return EmbeddingClient(
        spec,
        base_url=cfg["base_url"],
        api_key=_key(cfg),
        allow_external=cfg.get("provider") == "openai" or bool(cfg.get("allow_external")),
    )


def make_reranker(cfg: dict) -> Reranker:
    if (cfg.get("stub") and cfg["arg"] in ("", "noop")) or cfg.get("model") == "noop":
        return NoopReranker()
    if cfg.get("stub"):
        return StubReranker(cfg["arg"])
    raise ValueError(
        "no real reranker exists yet; implement the Reranker interface and add a factory"
    )


# ------------------------------------------------------------------------------ measurement


class Stats:
    def __init__(self) -> None:
        self.latency: list[float] = []
        self.t_in = 0
        self.t_out = 0
        self.failures = 0

    def timed(self, fn: Callable[[], Any], simulated_ms: float = 0.0) -> Any:
        t0 = time.perf_counter()
        try:
            return fn()
        except Exception:  # noqa: BLE001 - a failing provider call is a counted result
            self.failures += 1
            return None
        finally:
            self.latency.append((time.perf_counter() - t0) * 1000 + simulated_ms)

    def summary(self, prices: str, provider: str, model: str) -> dict[str, Any]:
        return {
            "latency_p50_ms": round(percentile(self.latency, 0.5), 3) if self.latency else 0.0,
            "latency_p95_ms": round(percentile(self.latency, 0.95), 3) if self.latency else 0.0,
            "tokens_in": self.t_in,
            "tokens_out": self.t_out,
            "est_cost_usd": estimate_cost_micros(prices, provider, model, self.t_in, self.t_out)
            / 1e6,
            "failures": self.failures,
        }


def _mean(vals: Sequence[float | None]) -> float:
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 6) if v else 0.0


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def _retrieval_metrics(rows: list[tuple[list[str], list[str]]]) -> dict[str, float]:
    """rows: (ranked unique-doc-order list of retrieved doc ids per chunk, relevant doc ids)."""
    return {
        f"hit@{K}": _mean([hit_at_k(r, rel, K) for r, rel in rows]),
        "mrr": _mean([reciprocal_rank(r, rel) for r, rel in rows]),
        "context_precision": _mean([rm.context_precision(r[:K], rel) for r, rel in rows]),
        "context_recall": _mean([rm.context_recall(r[:K], rel) for r, rel in rows]),
    }


def _index(docs: list[dict], embedder: Any, stats: Stats) -> list[dict]:
    """Temporary in-memory index: one entry per chunk, embedded in one batch."""
    chunks = [
        {"id": f"{d['id']}#{i}", "doc": d["id"], "ws": d["ws"], "text": t}
        for d in docs
        for i, t in enumerate(d["chunks"])
    ]
    vecs = stats.timed(
        lambda: embedder.embed_documents([c["text"] for c in chunks]),
        getattr(embedder, "simulated_ms", 0.0),
    )
    if vecs is None:
        return []
    stats.t_in += getattr(embedder, "last_tokens", 0) or sum(
        -(-len(c["text"]) // 4) for c in chunks
    )
    for c, v in zip(chunks, vecs, strict=True):
        c["vec"] = v
    return chunks


def _search(index: list[dict], qvec: Sequence[float], scope: str, n: int) -> list[dict]:
    pool = [c for c in index if c["ws"] in scope]
    return sorted(pool, key=lambda c: (-_cos(qvec, c["vec"]), c["id"]))[:n]


def _docs_in_order(chunks: Sequence[dict]) -> list[str]:
    seen: list[str] = []
    for c in chunks:
        if c["doc"] not in seen:
            seen.append(c["doc"])
    return seen


def run_embedding(cfg: dict, docs: list[dict], queries: list[dict], prices: str) -> dict[str, Any]:
    embedder = make_embedder(cfg)
    stats = Stats()
    index = _index(docs, embedder, stats)
    rows: list[tuple[list[str], list[str]]] = []
    for q in queries:
        qv = stats.timed(
            lambda q=q: embedder.embed_query(q["query"]), getattr(embedder, "simulated_ms", 0.0)
        )
        stats.t_in += -(-len(q["query"]) // 4)
        if qv is None or not index:
            rows.append(([], q["relevant"]))
            continue
        top = _search(index, qv, q["scope"], K * 2)
        rows.append(([c["doc"] for c in top[:K]], q["relevant"]))
    # hit@k / MRR use doc ids at chunk granularity de-duplicated in rank order
    dedup = [(list(dict.fromkeys(r)), rel) for r, rel in rows]
    m = _retrieval_metrics(rows)
    m["mrr"] = _mean([reciprocal_rank(r, rel) for r, rel in dedup])
    return {
        "metrics": m,
        **stats.summary(prices, cfg.get("provider", "local"), cfg.get("model", "embedder")),
    }


def run_rerank(cfg: dict, docs: list[dict], queries: list[dict], prices: str) -> dict[str, Any]:
    reranker = make_reranker(cfg)
    base = StubEmbedder(dimensions=64, noise=0.3)  # fixed first stage shared by A and B
    index = _index(docs, base, Stats())
    stats = Stats()
    rows: list[tuple[list[str], list[str]]] = []
    for q in queries:
        top = _search(index, base.embed_query(q["query"]), q["scope"], 10)
        by_id = {c["id"]: c for c in top}
        order = stats.timed(
            lambda q=q, top=top: reranker.rerank(q["query"], [(c["id"], c["text"]) for c in top]),
            getattr(reranker, "simulated_ms", 0.0),
        )
        ranked = [by_id[i] for i in order] if order else top
        rows.append(([c["doc"] for c in ranked[:K]], q["relevant"]))
    dedup = [(list(dict.fromkeys(r)), rel) for r, rel in rows]
    m = _retrieval_metrics(rows)
    m["mrr"] = _mean([reciprocal_rank(r, rel) for r, rel in dedup])
    return {"metrics": m, **stats.summary(prices, "rerank", cfg.get("model", "reranker"))}


def run_chat(cfg: dict, docs: list[dict], queries: list[dict], prices: str) -> dict[str, Any]:
    """Real prompt path: ``build_prompt`` -> chat model -> ``verify`` (strict) per question."""
    from app.modules.ai.context import build_prompt
    from app.modules.ai.verification import VerifyConfig, verify

    model = make_chat(cfg)
    by_doc = {d["id"]: d for d in docs}
    stats = Stats()
    grounded: list[float] = []
    faith: list[float | None] = []
    relevance: list[float | None] = []

    class Cit:  # citation shape expected by rag_metrics.faithfulness
        def __init__(self, n: int, cid: str):
            self.marker, self.chunk_id = f"[{n}]", cid

    for q in queries:
        ev = [
            Evidence(
                f"{d}#{i}",
                t,
                by_doc[d]["title"],
                1.0 - i * 0.01,
                document_id=d,
                chunk_id=f"{d}#{i}",
            )
            for d in q["relevant"]
            for i, t in enumerate(by_doc[d]["chunks"])
        ]
        bundle = build_prompt(q["query"], ev, budget_tokens=int(cfg.get("context_tokens", 8192)))
        if not bundle.should_call_model:
            grounded.append(0.0)
            continue
        text = stats.timed(
            lambda b=bundle: model.complete(
                b.messages, temperature=0.0, max_tokens=int(cfg.get("max_output_tokens", 512))
            ),
            getattr(model, "simulated_ms", 0.0),
        )
        usage = getattr(model, "last_usage", None) or {}
        stats.t_in += usage.get("prompt_tokens", bundle.estimated_tokens)
        stats.t_out += usage.get("completion_tokens", -(-len(text or "") // 4))
        if text is None:
            grounded.append(0.0)
            continue
        v = verify(text, bundle.citation_map, bundle.used_evidence, config=VerifyConfig())
        grounded.append(1.0 if v.status == "grounded" else 0.0)
        cmap = bundle.citation_map
        cites = [Cit(n, eid) for n, eid in cmap.items()]
        chunk_text = {e.evidence_id: e.text for e in bundle.used_evidence}
        abst = v.status == "abstained"
        faith.append(rm.faithfulness(text, cites, chunk_text, abstained=abst))
        relevance.append(rm.answer_relevance(q["query"], text, abstained=abst))
    m = {
        "grounded_rate": _mean(grounded),
        "faithfulness": _mean(faith),
        "answer_relevance": _mean(relevance),
    }
    return {
        "metrics": m,
        **stats.summary(prices, cfg.get("provider", "local"), cfg.get("model", "chat")),
    }


RUNNERS = {"chat": run_chat, "embedding": run_embedding, "rerank": run_rerank}


# ------------------------------------------------------------------------------ comparison


def compare(
    mode: str,
    a: dict,
    b: dict,
    *,
    max_quality_drop: float,
    max_latency_ratio: float,
    max_cost_ratio: float,
) -> dict[str, Any]:
    """B is the candidate, A the baseline. Regression = B worse than A beyond a threshold."""
    score_a, score_b = _mean(list(a["metrics"].values())), _mean(list(b["metrics"].values()))
    winner = "tie" if abs(score_a - score_b) <= TIE_EPS else ("A" if score_a > score_b else "B")
    reg: list[str] = []
    for name, va in a["metrics"].items():
        vb = b["metrics"][name]
        if va - vb > max_quality_drop:
            reg.append(f"{name} dropped {va:.3f} -> {vb:.3f}")
    if a["latency_p95_ms"] > 0 and b["latency_p95_ms"] > a["latency_p95_ms"] * max_latency_ratio:
        reg.append(
            f"p95 latency {a['latency_p95_ms']:.1f}ms -> {b['latency_p95_ms']:.1f}ms (> x{max_latency_ratio})"
        )
    if a["est_cost_usd"] > 0 and b["est_cost_usd"] > a["est_cost_usd"] * max_cost_ratio:
        reg.append(
            f"cost ${a['est_cost_usd']:.6f} -> ${b['est_cost_usd']:.6f} (> x{max_cost_ratio})"
        )
    if b["failures"] > a["failures"]:
        reg.append(f"failures {a['failures']} -> {b['failures']}")
    return {
        "mode": mode,
        "score": {"A": score_a, "B": score_b},
        "winner": winner,
        "regressions": reg,
    }


def render_md(report: dict[str, Any]) -> str:
    a, b = report["a"], report["b"]
    rows = [(k, a["metrics"][k], b["metrics"][k]) for k in a["metrics"]]
    rows += [
        (k, a[k], b[k])
        for k in (
            "latency_p50_ms",
            "latency_p95_ms",
            "tokens_in",
            "tokens_out",
            "est_cost_usd",
            "failures",
        )
    ]
    lines = [
        f"# Comparison: {report['mode']}",
        "",
        f"Dataset: {report['dataset']} ({report['questions']} questions). Winner: **{report['winner']}** "
        f"(score A {report['score']['A']:.3f}, B {report['score']['B']:.3f}).",
        "",
        f"| metric | A: {report['a_label']} | B: {report['b_label']} | delta (B-A) |",
        "|---|---|---|---|",
    ]
    for k, va, vb in rows:
        lines.append(f"| {k} | {va} | {vb} | {round(vb - va, 6)} |")
    lines += ["", "Regressions (B vs A): " + ("none" if not report["regressions"] else "")]
    lines += [f"- {r}" for r in report["regressions"]]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("mode", choices=sorted(RUNNERS))
    ap.add_argument("--a", required=True, help="baseline config")
    ap.add_argument("--b", required=True, help="candidate config")
    ap.add_argument(
        "--dataset", type=Path, default=None, help="dir with corpus.json + queries.json"
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--prices-json", default=os.environ.get("USAGE_PRICES_JSON", "{}"))
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--max-quality-drop", type=float, default=0.02)
    ap.add_argument("--max-latency-ratio", type=float, default=2.0)
    ap.add_argument("--max-cost-ratio", type=float, default=2.0)
    ap.add_argument("--fail-on-regression", action="store_true")
    args = ap.parse_args(argv)
    try:
        cfg_a, cfg_b = _parse(args.a), _parse(args.b)
        docs, queries = load_dataset(args.dataset)
        if args.limit:
            queries = queries[: args.limit]
        run = RUNNERS[args.mode]
        res_a, res_b = (
            run(cfg_a, docs, queries, args.prices_json),
            run(cfg_b, docs, queries, args.prices_json),
        )
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    verdict = compare(
        args.mode,
        res_a,
        res_b,
        max_quality_drop=args.max_quality_drop,
        max_latency_ratio=args.max_latency_ratio,
        max_cost_ratio=args.max_cost_ratio,
    )
    report = {
        **verdict,
        "dataset": str(args.dataset or GOLDEN.name),
        "questions": len(queries),
        "a_label": cfg_a.get("model", args.a),
        "b_label": cfg_b.get("model", args.b),
        "a": res_a,
        "b": res_b,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{args.mode}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = render_md(report)
    (args.out_dir / f"{args.mode}.md").write_text(md, encoding="utf-8")
    print(md)
    return 2 if (args.fail_on_regression and verdict["regressions"]) else 0


if __name__ == "__main__":
    sys.exit(main())
