"""Unit tests for the pure RAG metrics (M12.02) and the budget comparison (M12.04)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rag_metrics as rm


@dataclass
class Cit:
    marker: str
    chunk_id: str


@dataclass
class Info:
    state: str
    text: str


TEXT = "New engineers get a 14-inch laptop with 32 GB of memory."


def test_context_precision_recall() -> None:
    assert rm.context_precision(["a", "b", "a", "c"], ["a"]) == 0.5
    assert rm.context_precision([], ["a"]) is None
    assert rm.context_recall(["a", "b"], ["a", "z"]) == 0.5
    assert rm.context_recall(["a"], []) is None


def test_faithfulness_supported_and_unsupported() -> None:
    cits = [Cit("[1]", "c1")]
    chunks = {"c1": TEXT}
    assert rm.faithfulness("Engineers get a 14-inch laptop. [1]", cits, chunks) == 1.0
    assert rm.faithfulness("Engineers get a free car and a pony. [1]", cits, chunks) == 0.0
    assert rm.faithfulness("Engineers get a 14-inch laptop.", cits, chunks) == 0.0  # no marker
    half = "Engineers get a 14-inch laptop. [1] They also receive a yacht. [1]"
    assert rm.faithfulness(half, cits, chunks) == 0.5
    assert rm.faithfulness("I cannot answer.", [], chunks, abstained=True) is None


def test_faithfulness_pluggable_judge() -> None:
    class Never:
        def supports(self, claim: str, evidence: str) -> bool:
            return False

    got = rm.faithfulness("Laptop 14-inch. [1]", [Cit("[1]", "c1")], {"c1": TEXT}, judge=Never())
    assert got == 0.0


def test_answer_relevance() -> None:
    q = "What laptop do new engineers get?"
    assert rm.answer_relevance(q, "New engineers get a laptop. [1]") == 1.0
    assert rm.answer_relevance(q, "Unrelated text about budgets.") == 0.0
    assert rm.answer_relevance(q, "x", abstained=True) is None


def test_citation_validity() -> None:
    reg = {"c1": Info("published", TEXT), "c2": Info("superseded", TEXT)}
    ans = "Engineers get a 14-inch laptop. [1] Engineers get a 14-inch laptop. [2]"
    assert rm.citation_validity(ans, [Cit("[1]", "c1"), Cit("[2]", "c2")], reg) == (1, 2)
    assert rm.citation_validity(ans, [Cit("[1]", "zz")], reg) == (0, 1)  # unknown chunk
    assert rm.citation_validity("Yachts for everyone. [1]", [Cit("[1]", "c1")], reg) == (0, 1)
    assert rm.citation_validity("Engineers get laptops.", [], reg) == (0, 1)  # uncited answer
    assert rm.citation_validity("n/a", [], reg, abstained=True) == (0, 0)


def test_abstention_correct() -> None:
    assert rm.abstention_correct(True, True)
    assert rm.abstention_correct(False, False)
    assert not rm.abstention_correct(True, False)
    assert not rm.abstention_correct(False, True)


def test_budget_compare() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import budgets

    measured = {"latency_ms": {"p50": 10.0}, "tokens_per_answer": {"total": 120.0}}
    ok = budgets.compare(
        measured,
        {
            "latency_ms": {"p50": 5},
            "tokens_per_answer": {"total": 110},
            "tolerance": {"latency": 1.5},
        },
    )
    assert all(c["passed"] for c in ok)
    bad = budgets.compare(
        measured, {"tokens_per_answer": {"total": 100}, "tolerance": {"tokens": 0.1}}
    )
    assert not bad[0]["passed"]
