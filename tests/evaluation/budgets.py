"""Latency and cost budgets on the eval set (M12.04).

    python ../../tests/evaluation/budgets.py            # measure, compare with budgets.json
    python ../../tests/evaluation/budgets.py --update   # rewrite budgets.json from this run

Records per run: p50/p95 end-to-end latency per question (retrieval + answer, ms) and tokens per
answer. The offline stub has no model accounting, so tokens are a deterministic proxy computed here:
prompt tokens = word tokens of the question plus all retrieved chunk texts handed to the answerer,
completion tokens = word tokens of the answer text. A run passes when every measured value is at most
``budget * (1 + tolerance)``. Latency is machine dependent (wide tolerance); tokens are exact
(tight tolerance). Exit 0 pass, 1 over budget, 2 usage/data error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE / "metrics"), str(HERE), str(HERE.parents[1] / "apps" / "api")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import metrics as ir_metrics
from adapters import load_factory
from corpus_build import build_environment
from evaluate import run_items
from loader import DEFAULT_CORPUS, DEFAULT_DATASET, load_dataset
from report import Recorder

DEFAULT_BUDGETS = HERE / "budgets.json"
DEFAULT_TOLERANCE = {"latency": 3.0, "tokens": 0.1}
_TOKEN = re.compile(r"\w+|[^\w\s]")


def count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


def measure(corpus: Path = DEFAULT_CORPUS, dataset: Path = DEFAULT_DATASET) -> dict[str, Any]:
    env = build_environment(corpus)
    items = load_dataset(dataset, env.manifest)
    rec = Recorder(
        load_factory("adapters:baseline_retriever", env, "adapters"),
        load_factory("adapters:baseline_answerer", env, "adapters"),
    )
    run_items(items, env, rec, rec)
    latency = [c.retrieval_ms + c.answer_ms for c in rec.calls]
    prompt = [
        count_tokens(c.question) + sum(count_tokens(r.text) for r in c.retrieved) for c in rec.calls
    ]
    completion = [count_tokens(c.answer.text) for c in rec.calls]
    n = len(rec.calls)
    return {
        "items": n,
        "latency_ms": {
            "p50": round(ir_metrics.percentile(latency, 50), 3),
            "p95": round(ir_metrics.percentile(latency, 95), 3),
        },
        "tokens_per_answer": {
            "prompt": round(sum(prompt) / n, 1),
            "completion": round(sum(completion) / n, 1),
            "total": round((sum(prompt) + sum(completion)) / n, 1),
        },
    }


def compare(measured: dict[str, Any], budgets: dict[str, Any]) -> list[dict[str, Any]]:
    """One check per budgeted value: passed when measured <= budget * (1 + tolerance)."""
    tol = {**DEFAULT_TOLERANCE, **budgets.get("tolerance", {})}
    checks: list[dict[str, Any]] = []
    for section, tol_key in (("latency_ms", "latency"), ("tokens_per_answer", "tokens")):
        for name, budget in budgets.get(section, {}).items():
            value = measured[section][name]
            limit = budget * (1 + tol[tol_key])
            checks.append(
                {
                    "name": f"{section}.{name}",
                    "value": value,
                    "budget": budget,
                    "limit": round(limit, 3),
                    "passed": value <= limit,
                }
            )
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budgets", type=Path, default=DEFAULT_BUDGETS)
    ap.add_argument("--update", action="store_true", help="write budgets.json from this run")
    args = ap.parse_args(argv)
    measured = measure()
    if args.update:
        out = {
            "schema_version": 1,
            "tolerance": DEFAULT_TOLERANCE,
            **{k: measured[k] for k in ("latency_ms", "tokens_per_answer")},
        }
        args.budgets.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"budgets written: {args.budgets}")
        return 0
    if not args.budgets.exists():
        print(f"error: {args.budgets} missing (run with --update)", file=sys.stderr)
        return 2
    checks = compare(measured, json.loads(args.budgets.read_text(encoding="utf-8")))
    for c in checks:
        print(
            f"{'ok  ' if c['passed'] else 'OVER'} {c['name']} = {c['value']} (limit {c['limit']})"
        )
    ok = all(c["passed"] for c in checks)
    print("BUDGETS " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
