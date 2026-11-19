"""Unit tests of the pure metrics and of the per-item checks, with hand-computed expectations."""

from __future__ import annotations

import math

import metrics
import pytest
from adapters import Answer, Citation, RetrievedChunk
from corpus_build import ChunkInfo
from evaluate import aggregate, check_item
from loader import Item


def test_recall_at_k() -> None:
    assert metrics.recall_at_k(["a", "b", "c"], ["b", "d"], 2) == 0.5
    assert metrics.recall_at_k(["a", "b", "c"], ["b", "d"], 1) == 0.0
    assert metrics.recall_at_k(["a"], [], 5) == 0.0


def test_reciprocal_rank_and_hit() -> None:
    assert metrics.reciprocal_rank(["a", "b", "c"], ["c", "b"]) == 0.5
    assert metrics.reciprocal_rank(["a"], ["z"]) == 0.0
    assert metrics.hit_at_k(["a", "b"], ["b"], 1) == 0.0
    assert metrics.hit_at_k(["a", "b"], ["b"], 2) == 1.0


def test_ndcg() -> None:
    # relevant doc at rank 2: DCG = 1/log2(3); ideal = 1
    assert metrics.ndcg_at_k(["a", "b", "c"], ["b"], 10) == pytest.approx(1 / math.log2(3))
    assert metrics.ndcg_at_k(["b", "a"], ["a", "b"], 10) == pytest.approx(1.0)
    # two relevant at ranks 1 and 3: DCG = 1 + 1/log2(4) = 1.5; ideal = 1 + 1/log2(3)
    assert metrics.ndcg_at_k(["a", "x", "b"], ["a", "b"], 10) == pytest.approx(
        1.5 / (1 + 1 / math.log2(3))
    )
    assert metrics.ndcg_at_k(["x"], ["a"], 10) == 0.0


def test_percentiles_use_nearest_rank() -> None:
    values = [float(v) for v in range(1, 11)]
    assert metrics.percentile(values, 50) == 5.0
    assert metrics.percentile(values, 95) == 10.0
    assert metrics.percentile([], 50) == 0.0
    with pytest.raises(ValueError, match="within"):
        metrics.percentile(values, 101)
    assert metrics.latency_summary(values)["max"] == 10.0


def test_abstention_scores() -> None:
    scores = metrics.abstention_scores([True, True, False, False], [True, False, True, False])
    assert scores == {"precision": 0.5, "recall": 0.5}
    # never abstains: precision undefined -> 1.0, recall 0
    never = metrics.abstention_scores([True, False], [False, False])
    assert never == {"precision": 1.0, "recall": 0.0}
    with pytest.raises(ValueError, match="same length"):
        metrics.abstention_scores([True], [])


def test_normalize_and_contains() -> None:
    assert metrics.contains("The  limit is\n 14\u00a0Characters.", "14 characters")
    assert not metrics.contains("fourteen", "14")


def _registry() -> dict[str, ChunkInfo]:
    def info(cid: str, doc: str, ws: str, state: str = "published", text: str = "") -> ChunkInfo:
        return ChunkInfo(cid, doc, ws, 1, state, text or f"text of {doc}", "h" + cid)  # type: ignore[arg-type]

    return {
        "c1": info("c1", "d1", "w1", text="Alpha fact is 5."),
        "c2": info("c2", "d2", "w2", text="Secret fact."),
        "c3": info("c3", "d3", "w1", state="superseded", text="Old fact."),
    }


def _chunk(cid: str, doc: str, ws: str) -> RetrievedChunk:
    return RetrievedChunk(cid, doc, ws, 1, "t", 1.0)


ITEM = Item("i", "q?", "answerable", "p", ("d1",), ("5",), ("Secret",), False)


def test_check_item_clean_answer() -> None:
    answer = Answer("Alpha fact is 5. [1]", [Citation("[1]", "c1", "d1", "Alpha fact is 5.")])
    r = check_item(ITEM, frozenset({"w1"}), [_chunk("c1", "d1", "w1")], answer, _registry())
    assert r["citations_valid"] == 1
    assert r["leak_chunks"] == 0
    assert r["correct"]
    assert r["recall@5"] == 1.0
    assert r["rr"] == 1.0


def test_check_item_detects_leaks_stale_and_bad_citations() -> None:
    answer = Answer(
        "Secret fact. [1] Old fact. [2] x [3] y [4]",
        [
            Citation("[1]", "c2", "d2", "Secret fact."),  # unauthorized
            Citation("[2]", "c3", "d3", "Old fact."),  # superseded
            Citation("[3]", "c1", "d9", "Alpha"),  # wrong document
            Citation("[4]", "c1", "d1", "invented text"),  # excerpt not in chunk
        ],
    )
    retrieved = [_chunk("c1", "d1", "w1"), _chunk("c2", "d2", "w2"), _chunk("c3", "d3", "w1")]
    r = check_item(ITEM, frozenset({"w1"}), retrieved, answer, _registry())
    assert r["leak_chunks"] == 1
    assert r["leak_citations"] == 1
    assert r["stale_exposure"] == 2  # retrieved superseded chunk + cited superseded chunk
    assert r["invalid_reasons"] == [
        "unauthorized",
        "stale_superseded",
        "wrong_document",
        "excerpt_not_in_chunk",
    ]
    assert r["citations_valid"] == 0
    assert r["forbidden_hits"] == ["Secret"]
    assert not r["correct"]


def test_uncited_answer_and_unknown_chunk_are_invalid() -> None:
    r = check_item(ITEM, frozenset({"w1"}), [], Answer("5", [], "answered"), _registry())
    assert r["uncited_answer"]
    assert r["invalid_reasons"] == ["answer_without_citation"]
    ghost = Answer("5 [1]", [Citation("[1]", "nope", "d1", "5")])
    assert check_item(ITEM, frozenset({"w1"}), [], ghost, _registry())["invalid_reasons"] == [
        "unknown_chunk"
    ]


def test_aggregate_hand_computed() -> None:
    abstain_item = Item("u", "q?", "unanswerable", "p", (), (), (), True)
    good = check_item(
        ITEM,
        frozenset({"w1"}),
        [_chunk("c1", "d1", "w1")],
        Answer("Alpha fact is 5. [1]", [Citation("[1]", "c1", "d1", "Alpha fact is 5.")]),
        _registry(),
    )
    wrong_abstain = check_item(
        ITEM, frozenset({"w1"}), [], Answer("no", [], "abstained"), _registry()
    )
    right_abstain = check_item(
        abstain_item, frozenset({"w1"}), [], Answer("no", [], "abstained"), _registry()
    )
    for r in (good, wrong_abstain, right_abstain):
        r["retrieval_ms"] = 1.0
        r["answer_ms"] = 1.0
    agg = aggregate([good, wrong_abstain, right_abstain])
    assert agg["abstention_precision"] == 0.5  # 1 of 2 abstentions was right
    assert agg["abstention_recall"] == 1.0
    assert agg["answer_accuracy"] == 0.5  # 1 of 2 answerable items correct
    assert agg["recall@5"] == 0.5  # items with expected docs: (1.0 + 0.0) / 2
    assert agg["permission_leakage"] == 0
    assert agg["citation_validity"] == 1.0
