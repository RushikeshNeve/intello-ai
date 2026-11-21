"""Offline RAG evaluation and CI gate (M04.03.05).

From ``apps/api`` (so ``app.*`` imports):

    python ../../tests/evaluation/run_eval.py                    # gate against baseline.json
    python ../../tests/evaluation/run_eval.py --update-baseline  # explicit baseline change
    python ../../tests/evaluation/run_eval.py \
        --retriever app.modules.retrieval.eval_adapter:make_retriever \
        --answerer app.modules.ai.eval_adapter:make_answerer

Writes ``reports/eval.json`` (machine readable) and ``reports/eval.md``. Exit code 0 = gate passed,
1 = gate failed, 2 = usage or data error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1] / "apps" / "api"))  # `app.*`, whatever the cwd

import gate
from adapters import Answerer, Retriever, load_factory
from corpus_build import EvalEnv, build_environment
from evaluate import aggregate, run_items
from loader import DEFAULT_CORPUS, DEFAULT_DATASET, DatasetError, load_dataset

REPORT_SCHEMA_VERSION = 1
DEFAULT_RETRIEVER = "adapters:baseline_retriever"
DEFAULT_ANSWERER = "adapters:baseline_answerer"
DEFAULT_BASELINE = HERE / "baseline.json"
DEFAULT_REPORTS = HERE / "reports"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corpus_sha256(env: EvalEnv) -> str:
    digest = hashlib.sha256()
    digest.update((env.manifest.root / "manifest.json").read_bytes())
    for doc in env.manifest.documents:
        for version in doc.versions:
            digest.update(f"{doc.id}:{version.version}".encode())
            digest.update(env.manifest.read(version))
    return digest.hexdigest()


def run_eval(
    *,
    corpus_dir: Path = DEFAULT_CORPUS,
    dataset_path: Path = DEFAULT_DATASET,
    retriever_spec: str = DEFAULT_RETRIEVER,
    answerer_spec: str = DEFAULT_ANSWERER,
    baseline_path: Path = DEFAULT_BASELINE,
    require_baseline: bool = False,
    retriever: Retriever | None = None,
    answerer: Answerer | None = None,
    rag_metrics: bool = False,
) -> dict[str, Any]:
    """Build the corpus, run every item and return the full report (including the gate verdict)."""
    env = build_environment(corpus_dir)
    items = load_dataset(dataset_path, env.manifest)
    if retriever is None:
        retriever = load_factory(retriever_spec, env, "adapters")
    if answerer is None:
        answerer = load_factory(answerer_spec, env, "adapters")
    recorder = None
    if rag_metrics:  # M12.02: record calls to score faithfulness/citations; gate is unaffected
        sys.path.insert(0, str(HERE / "metrics"))
        from report import Recorder

        recorder = Recorder(retriever, answerer)
        retriever, answerer = recorder, recorder
    results = run_items(items, env, retriever, answerer)
    agg = aggregate(results)
    dataset_hash = sha256_file(dataset_path)
    corpus_hash = corpus_sha256(env)
    key = gate.run_key(retriever_spec, answerer_spec)
    verdict = gate.evaluate_gate(
        agg,
        baseline=gate.load_baseline(baseline_path),
        key=key,
        dataset_sha256=dataset_hash,
        corpus_sha256=corpus_hash,
        require_baseline=require_baseline,
    )
    extra: dict[str, Any] = {}
    if recorder is not None:
        from report import aggregate_rag, per_item

        rows = per_item(items, recorder.calls, env)
        extra["rag_metrics"] = {"aggregate": aggregate_rag(rows), "items": rows}
    return {
        **extra,
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "offline",
        "adapters": {"retriever": retriever_spec, "answerer": answerer_spec},
        "dataset": {"path": dataset_path.name, "sha256": dataset_hash, "items": len(items)},
        "corpus": {
            "name": env.manifest.name,
            "sha256": corpus_hash,
            "documents": len(env.manifest.documents),
            "chunks": {s: env.chunk_count(s) for s in ("published", "superseded", "deleted")},
        },
        "metrics": agg,
        "gate": verdict,
        "items": results,
    }


_PCT = ("p50", "p95", "p99")


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    g = report["gate"]
    lines = [
        "# RAG evaluation report",
        "",
        f"- Gate: **{'PASSED' if g['passed'] else 'FAILED'}** (baseline {g['baseline']['status']})",
        f"- Adapters: `{report['adapters']['retriever']}` / `{report['adapters']['answerer']}`",
        f"- Dataset: {report['dataset']['items']} items, "
        f"corpus {report['corpus']['documents']} documents ({report['corpus']['chunks']})",
        "",
        "## Gate checks",
        "",
        "| check | type | value | required | result |",
        "|---|---|---|---|---|",
    ]
    for c in g["checks"]:
        value = c["value"]
        shown = f"{value:.4f}" if isinstance(value, float) else str(value)
        result = "ok" if c["passed"] else "FAIL"
        lines.append(f"| {c['name']} | {c['type']} | {shown} | {c['required']} | {result} |")
    lines += ["", "## Metrics", "", "| metric | value |", "|---|---|"]
    for name in (
        "recall@5",
        "recall@10",
        "mrr",
        "ndcg@10",
        "hit_rate@5",
        "answer_accuracy",
        "abstention_precision",
        "abstention_recall",
        "citation_validity",
        "injection_resistance",
        "updated_fact_correctness",
        "permission_leakage",
        "stale_exposure",
    ):
        value = m[name]
        lines.append(
            f"| {name} | {value:.4f} |" if isinstance(value, float) else f"| {name} | {value} |"
        )
    lines += ["", "## By kind", "", "| kind | items | correct |", "|---|---|---|"]
    for kind, row in m["by_kind"].items():
        lines.append(f"| {kind} | {row['items']} | {row['correct_rate']:.3f} |")
    lat = m["latency_ms"]
    lines += [
        "",
        "## Latency (ms, informational)",
        "",
        "- retrieval p50/p95/p99: " + " / ".join(f"{lat['retrieval'][p]:.2f}" for p in _PCT),
        "- answer p50/p95/p99: " + " / ".join(f"{lat['answer'][p]:.2f}" for p in _PCT),
    ]
    failing = [r for r in report["items"] if not r["correct"]]
    lines += ["", f"## Incorrect items ({len(failing)})", ""]
    for r in failing:
        why = (
            r["facts_missing"]
            or r["forbidden_hits"]
            or ("abstained" if r["abstained"] else "answered")
        )
        lines.append(f"- `{r['id']}` ({r['kind']}, {r['persona']}): {why}")
    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "eval.json"
    md_path = directory / "eval.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _live_guard() -> int:
    """``--live`` is a documented stub: it refuses unless explicitly enabled and says why."""
    if os.environ.get("EVAL_ALLOW_LIVE") != "1":
        print(
            "--live refused: set EVAL_ALLOW_LIVE=1 to opt in (live mode is never required).",
            file=sys.stderr,
        )
        return 2
    external = bool(os.environ.get("OPENAI_API_KEY")) and (
        os.environ.get("AI_EXTERNAL_PROVIDERS_ENABLED", "").lower() == "true"
    )
    judge = "openai" if external else "local (LM Studio)"
    print(
        f"--live: judge would be {judge}, but live mode is not implemented yet (M04 gate batch).",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--retriever", default=DEFAULT_RETRIEVER, help="pkg.mod:factory(env) -> Retriever"
    )
    parser.add_argument(
        "--answerer", default=DEFAULT_ANSWERER, help="pkg.mod:factory(env) -> Answerer"
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORTS)
    parser.add_argument(
        "--require-baseline", action="store_true", help="fail when no baseline entry exists (CI)"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write the current numbers as the new baseline",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="also write reports/metrics.json + metrics.md (RAG metrics; never changes the gate)",
    )
    parser.add_argument(
        "--live", action="store_true", help="optional live model run (stub; see docs)"
    )
    args = parser.parse_args(argv)
    if args.live:
        return _live_guard()
    try:
        report = run_eval(
            corpus_dir=args.corpus,
            dataset_path=args.dataset,
            retriever_spec=args.retriever,
            answerer_spec=args.answerer,
            baseline_path=args.baseline,
            require_baseline=args.require_baseline and not args.update_baseline,
            rag_metrics=args.metrics,
        )
    except (DatasetError, ImportError, AttributeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.update_baseline:
        key = gate.run_key(args.retriever, args.answerer)
        new = gate.updated_baseline(
            gate.load_baseline(args.baseline),
            key=key,
            agg=report["metrics"],
            dataset_sha256=report["dataset"]["sha256"],
            corpus_sha256=report["corpus"]["sha256"],
        )
        args.baseline.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline updated for {key}")
        # Hard gates still apply to the report that was just produced.
        report["gate"] = gate.evaluate_gate(
            report["metrics"],
            baseline=new,
            key=key,
            dataset_sha256=report["dataset"]["sha256"],
            corpus_sha256=report["corpus"]["sha256"],
            require_baseline=True,
        )
    json_path, md_path = write_reports(report, args.report_dir)
    if args.metrics:
        from report import render_markdown as rag_md

        rag = report["rag_metrics"]
        (args.report_dir / "metrics.json").write_text(
            json.dumps(rag, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (args.report_dir / "metrics.md").write_text(rag_md(rag["aggregate"]), encoding="utf-8")
    verdict = report["gate"]
    for c in verdict["checks"]:
        if not c["passed"]:
            print(
                f"GATE FAIL: {c['name']} value={c['value']} required {c['required']}",
                file=sys.stderr,
            )
    print(f"{'PASSED' if verdict['passed'] else 'FAILED'}; reports: {json_path} {md_path}")
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
