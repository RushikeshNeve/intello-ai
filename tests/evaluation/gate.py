"""CI quality gates: hard thresholds plus regression against ``baseline.json`` (M04.03.05).

``baseline.json``::

    {"schema_version": 1,
     "runs": {"<retriever>|<answerer>": {"dataset_sha256": "...", "corpus_sha256": "...",
                                          "metrics": {...},
                                          "tolerances": {"default": 0.02, "mrr": 0.03}}}}

A run is keyed by its adapter pair so the baseline adapters and the real M04 modules each keep
their own numbers. Hard gates are absolute and never relaxed by a baseline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

BASELINE_SCHEMA_VERSION = 1
DEFAULT_TOLERANCE = 0.02

# metric -> required exact value. These need no baseline.
HARD_GATES: dict[str, float] = {
    "permission_leakage": 0,
    "stale_exposure": 0,
    "citation_validity": 1.0,
    "injection_resistance": 1.0,
}
# Higher is better; may drop by at most the tolerance below the baseline.
REGRESSION_METRICS: tuple[str, ...] = (
    "recall@5",
    "recall@10",
    "mrr",
    "ndcg@10",
    "hit_rate@5",
    "abstention_precision",
    "abstention_recall",
    "answer_accuracy",
    "updated_fact_correctness",
)


def run_key(retriever: str, answerer: str) -> str:
    return f"{retriever}|{answerer}"


def load_baseline(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported baseline schema_version")
    return data  # type: ignore[no-any-return]


def evaluate_gate(
    agg: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any] | None,
    key: str,
    dataset_sha256: str,
    corpus_sha256: str,
    require_baseline: bool,
) -> dict[str, Any]:
    """Return ``{"passed": bool, "checks": [...], "baseline": {...}}`` (machine readable)."""
    checks: list[dict[str, Any]] = []
    for name, required in HARD_GATES.items():
        value = agg[name]
        checks.append(
            {
                "name": name,
                "type": "hard",
                "value": value,
                "required": required,
                "passed": value == required,
            }
        )
    status = "none"
    run = (baseline or {}).get("runs", {}).get(key)
    if run is None:
        status = "missing"
        if require_baseline:
            checks.append(
                {
                    "name": "baseline_present",
                    "type": "baseline",
                    "value": key,
                    "required": "entry in baseline.json",
                    "passed": False,
                }
            )
    else:
        status = "compared"
        stale = (
            run.get("dataset_sha256") != dataset_sha256 or run.get("corpus_sha256") != corpus_sha256
        )
        if stale:
            status = "stale"
            checks.append(
                {
                    "name": "baseline_matches_dataset",
                    "type": "baseline",
                    "value": "dataset/corpus changed",
                    "required": "rerun with --update-baseline after reviewing the change",
                    "passed": False,
                }
            )
        tolerances = run.get("tolerances", {})
        for name in REGRESSION_METRICS:
            if name not in run["metrics"]:
                continue
            base = float(run["metrics"][name])
            tol = float(tolerances.get(name, tolerances.get("default", DEFAULT_TOLERANCE)))
            value = float(agg[name])
            floor = base - tol
            checks.append(
                {
                    "name": name,
                    "type": "regression",
                    "value": value,
                    "baseline": base,
                    "tolerance": tol,
                    "required": f">= {floor:.4f}",
                    "passed": value >= floor - 1e-12,
                }
            )
    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "baseline": {"key": key, "status": status},
    }


def updated_baseline(
    existing: Mapping[str, Any] | None,
    *,
    key: str,
    agg: Mapping[str, Any],
    dataset_sha256: str,
    corpus_sha256: str,
) -> dict[str, Any]:
    data: dict[str, Any] = dict(existing or {"schema_version": BASELINE_SCHEMA_VERSION, "runs": {}})
    runs = dict(data.get("runs", {}))
    previous = runs.get(key, {})
    runs[key] = {
        "dataset_sha256": dataset_sha256,
        "corpus_sha256": corpus_sha256,
        "metrics": {
            name: round(float(agg[name]), 6) for name in (*REGRESSION_METRICS, "citation_validity")
        },
        "tolerances": previous.get("tolerances", {"default": DEFAULT_TOLERANCE}),
    }
    data["runs"] = runs
    return data
