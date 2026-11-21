# ruff: noqa: PT018
"""M12.08: the harness ranks a better config above a worse one and flags regressions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_compare as rc


def _run(tmp_path, mode, a, b, *extra):
    code = rc.main(
        [
            mode,
            "--a",
            a,
            "--b",
            b,
            "--out-dir",
            str(tmp_path),
            "--max-latency-ratio",
            "1000",
            *extra,
        ]
    )
    return code, json.loads((tmp_path / f"{mode}.json").read_text(encoding="utf-8"))


def test_chat_better_config_wins_and_regression_flagged(tmp_path) -> None:
    code, rep = _run(tmp_path, "chat", "stub:0.2", "stub:1.0")
    assert rep["winner"] == "B" and not any("grounded_rate" in r for r in rep["regressions"])
    code, rep = _run(tmp_path, "chat", "stub:1.0", "stub:0.2", "--fail-on-regression")
    assert rep["winner"] == "A" and code == 2
    assert any("grounded_rate" in r for r in rep["regressions"])
    assert (tmp_path / "chat.md").read_text(encoding="utf-8").startswith("# Comparison: chat")


def test_embedding_noisy_loses_and_report_columns(tmp_path) -> None:
    code, rep = _run(tmp_path, "embedding", "stub:0.0", "stub:0.9", "--fail-on-regression")
    assert rep["winner"] == "A" and code == 2
    assert rep["a"]["metrics"]["hit@5"] > rep["b"]["metrics"]["hit@5"]
    for col in (
        "latency_p50_ms",
        "latency_p95_ms",
        "tokens_in",
        "tokens_out",
        "est_cost_usd",
        "failures",
    ):
        assert col in rep["a"]


def test_rerank_noop_baseline_vs_reverse(tmp_path) -> None:
    code, rep = _run(tmp_path, "rerank", "noop", "stub:reverse", "--fail-on-regression")
    assert code == 2 and rep["winner"] == "A"


def test_failures_and_cost_are_reported(tmp_path) -> None:
    prices = json.dumps({"stub/*": {"in": 1_000_000, "out": 2_000_000}})
    code, rep = _run(tmp_path, "chat", "stub:1.0", "stub:1.0", "--prices-json", prices)
    assert (
        code == 0
        and rep["winner"] == "tie"
        and rep["regressions"] == []
        and rep["a"]["est_cost_usd"] > 0
    )
    assert rc.estimate_cost_micros(prices, "stub", "x", 1000, 1000) == 3000
    assert rc.estimate_cost_micros("not json", "p", "m", 5, 5) == 0


def test_bad_config_exit_1(tmp_path) -> None:
    assert rc.main(["chat", "--a", "bogus", "--b", "stub:1", "--out-dir", str(tmp_path)]) == 1
