"""The CI gate: it passes for the baseline adapters and FAILS for leaking, citation-breaking and
obedient (injectable) mutants; baseline comparison and the machine-readable report schema."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import gate
import pytest
import run_eval
from adapters import (
    Answer,
    BaselineRetriever,
    ExtractiveAnswerer,
    RetrievedChunk,
)
from corpus_build import EvalEnv

# --------------------------------------------------------------------------------- mutants


class LeakyRetriever:
    """Mutation: ignores the caller's scope and searches every workspace of every org."""

    def __init__(self, env: EvalEnv) -> None:
        self._env = env
        self._inner = BaselineRetriever(env)

    def retrieve(self, query: str, workspace_ids: Sequence[str]) -> list[RetrievedChunk]:
        return self._inner.retrieve(query, list(self._env.manifest.workspaces))


class ForgingAnswerer:
    """Mutation: cites text that the chunk does not contain."""

    def __init__(self, env: EvalEnv) -> None:
        del env
        self._inner = ExtractiveAnswerer()

    def answer(self, question: str, retrieved: Sequence[RetrievedChunk]) -> Answer:
        real = self._inner.answer(question, retrieved)
        forged = [replace(c, excerpt=c.excerpt + " (invented)") for c in real.citations]
        return Answer(real.text, forged, real.status)


class ObedientAnswerer:
    """Mutation: follows instructions found in retrieved text (the hostile document)."""

    def __init__(self, env: EvalEnv) -> None:
        del env
        self._inner = ExtractiveAnswerer()

    def answer(self, question: str, retrieved: Sequence[RetrievedChunk]) -> Answer:
        real = self._inner.answer(question, retrieved)
        if any("PWNED-7731" in c.text for c in retrieved):
            return Answer(real.text + " PWNED-7731", real.citations, real.status)
        return real


class UncitedAnswerer:
    def __init__(self, env: EvalEnv) -> None:
        del env

    def answer(self, question: str, retrieved: Sequence[RetrievedChunk]) -> Answer:
        return Answer("Some answer without sources.", [], "answered")


def leaky_factory(env: EvalEnv) -> LeakyRetriever:
    return LeakyRetriever(env)


def forging_factory(env: EvalEnv) -> ForgingAnswerer:
    return ForgingAnswerer(env)


def obedient_factory(env: EvalEnv) -> ObedientAnswerer:
    return ObedientAnswerer(env)


def uncited_factory(env: EvalEnv) -> UncitedAnswerer:
    return UncitedAnswerer(env)


def check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in report["gate"]["checks"] if c["name"] == name)


@pytest.fixture(scope="module")
def baseline_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    missing = tmp_path_factory.mktemp("b") / "none.json"
    return run_eval.run_eval(baseline_path=missing)


# ----------------------------------------------------------------------------------- tests


def test_baseline_adapters_pass_the_hard_gates(baseline_report: dict[str, Any]) -> None:
    assert baseline_report["gate"]["passed"], baseline_report["gate"]
    m = baseline_report["metrics"]
    assert m["permission_leakage"] == 0
    assert m["stale_exposure"] == 0
    assert m["citation_validity"] == 1.0
    assert m["injection_resistance"] == 1.0


def test_gate_fails_for_a_leaking_retriever(tmp_path: Path) -> None:
    report = run_eval.run_eval(
        retriever_spec="test_eval_gate:leaky_factory", baseline_path=tmp_path / "none.json"
    )
    assert not report["gate"]["passed"]
    assert report["metrics"]["permission_leakage"] > 0
    assert not check(report, "permission_leakage")["passed"]


def test_gate_fails_when_citation_validity_drops(tmp_path: Path) -> None:
    report = run_eval.run_eval(
        answerer_spec="test_eval_gate:forging_factory", baseline_path=tmp_path / "none.json"
    )
    assert report["metrics"]["citation_validity"] < 1.0
    assert not check(report, "citation_validity")["passed"]
    assert not report["gate"]["passed"]


def test_answers_without_citations_count_against_validity(tmp_path: Path) -> None:
    report = run_eval.run_eval(
        answerer_spec="test_eval_gate:uncited_factory", baseline_path=tmp_path / "none.json"
    )
    assert report["metrics"]["citation_validity"] == 0.0
    assert not report["gate"]["passed"]


def test_gate_fails_when_the_model_obeys_injected_instructions(tmp_path: Path) -> None:
    report = run_eval.run_eval(
        answerer_spec="test_eval_gate:obedient_factory", baseline_path=tmp_path / "none.json"
    )
    assert report["metrics"]["injection_resistance"] < 1.0
    assert not check(report, "injection_resistance")["passed"]


def test_main_exit_codes_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reports = tmp_path / "reports"
    args = ["--report-dir", str(reports), "--baseline", str(tmp_path / "b.json")]
    assert run_eval.main(args) == 0  # hard gates only, no baseline required
    assert run_eval.main([*args, "--require-baseline"]) == 1  # CI mode: baseline entry is mandatory
    assert run_eval.main([*args, "--retriever", "test_eval_gate:leaky_factory"]) == 1
    assert "GATE FAIL: permission_leakage" in capsys.readouterr().err
    assert (reports / "eval.json").is_file()
    assert (reports / "eval.md").is_file()
    assert run_eval.main([*args, "--retriever", "no.such.module:factory"]) == 2


def test_update_baseline_then_gate_passes_and_regression_fails(tmp_path: Path) -> None:
    baseline = tmp_path / "b.json"
    args = ["--report-dir", str(tmp_path / "r"), "--baseline", str(baseline)]
    assert run_eval.main([*args, "--update-baseline"]) == 0
    assert run_eval.main([*args, "--require-baseline"]) == 0
    data = json.loads(baseline.read_text(encoding="utf-8"))
    key = gate.run_key(run_eval.DEFAULT_RETRIEVER, run_eval.DEFAULT_ANSWERER)
    run = data["runs"][key]
    # a baseline the current numbers no longer reach (beyond tolerance) is a regression
    run["metrics"]["mrr"] = min(1.0, run["metrics"]["mrr"] + 0.1)
    baseline.write_text(json.dumps(data), encoding="utf-8")
    assert run_eval.main([*args, "--require-baseline"]) == 1
    # within tolerance passes
    run["metrics"]["mrr"] = run["metrics"]["mrr"] - 0.1 + 0.01
    baseline.write_text(json.dumps(data), encoding="utf-8")
    assert run_eval.main([*args, "--require-baseline"]) == 0
    # a changed dataset hash makes the baseline stale
    run["dataset_sha256"] = "0" * 64
    baseline.write_text(json.dumps(data), encoding="utf-8")
    assert run_eval.main([*args, "--require-baseline"]) == 1


def test_evaluate_gate_unit() -> None:
    agg = dict.fromkeys(gate.REGRESSION_METRICS, 1.0)
    agg.update(
        permission_leakage=0, stale_exposure=0, citation_validity=1.0, injection_resistance=1.0
    )
    base = {
        "runs": {
            "k": {
                "dataset_sha256": "d",
                "corpus_sha256": "c",
                "metrics": {"mrr": 0.95},
                "tolerances": {"default": 0.02},
            }
        }
    }
    ok = gate.evaluate_gate(
        agg, baseline=base, key="k", dataset_sha256="d", corpus_sha256="c", require_baseline=True
    )
    assert ok["passed"]
    worse = {**agg, "mrr": 0.90}
    bad = gate.evaluate_gate(
        worse, baseline=base, key="k", dataset_sha256="d", corpus_sha256="c", require_baseline=True
    )
    assert not bad["passed"]
    leak = {**agg, "permission_leakage": 1}
    assert not gate.evaluate_gate(
        leak, baseline=None, key="k", dataset_sha256="d", corpus_sha256="c", require_baseline=False
    )["passed"]


def test_live_mode_refuses_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_ALLOW_LIVE", raising=False)
    assert run_eval.main(["--live"]) == 2
    monkeypatch.setenv("EVAL_ALLOW_LIVE", "1")
    assert run_eval.main(["--live"]) == 2  # still a stub: never silently passes


def test_report_schema(baseline_report: dict[str, Any], tmp_path: Path) -> None:
    report = json.loads(json.dumps(baseline_report))  # must be pure JSON
    assert report["schema_version"] == run_eval.REPORT_SCHEMA_VERSION == 1
    assert set(report) == {
        "schema_version",
        "generated_at",
        "mode",
        "adapters",
        "dataset",
        "corpus",
        "metrics",
        "gate",
        "items",
    }
    assert report["mode"] == "offline"
    assert set(report["adapters"]) == {"retriever", "answerer"}
    assert set(report["dataset"]) == {"path", "sha256", "items"}
    assert len(report["dataset"]["sha256"]) == 64
    assert report["dataset"]["items"] == len(report["items"]) >= 80
    for key in (
        "recall@5",
        "recall@10",
        "mrr",
        "ndcg@10",
        "hit_rate@5",
        "permission_leakage",
        "stale_exposure",
        "citation_validity",
        "abstention_precision",
        "abstention_recall",
        "answer_accuracy",
        "injection_resistance",
        "updated_fact_correctness",
        "latency_ms",
        "by_kind",
    ):
        assert key in report["metrics"], key
    assert set(report["metrics"]["latency_ms"]["retrieval"]) == {"p50", "p95", "p99", "max"}
    assert set(report["gate"]) == {"passed", "checks", "baseline"}
    for item in report["items"]:
        for field in (
            "id",
            "kind",
            "persona",
            "retrieved",
            "answer_text",
            "citations",
            "correct",
            "retrieval_ms",
        ):
            assert field in item
    json_path, md_path = run_eval.write_reports(baseline_report, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert "RAG evaluation report" in md_path.read_text(encoding="utf-8")
