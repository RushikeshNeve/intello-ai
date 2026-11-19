"""Fusion math, de-duplication, diversification and reranker guard (pure)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.modules.knowledge.ports import SearchHit
from app.modules.retrieval.ranking import (
    Candidate,
    apply_reranker,
    deduplicate,
    diversify,
    fuse,
    normalize_score,
)


def hit(
    text: str = "x",
    *,
    doc: uuid.UUID | None = None,
    start: int = 0,
    end: int | None = None,
    chunk: uuid.UUID | None = None,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk or uuid.uuid4(),
        document_id=doc or uuid.uuid4(),
        version_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        score=0.0,
        text=text,
        locators=[
            {
                "kind": "line",
                "char_start": start,
                "char_end": end if end is not None else start + len(text),
            }
        ],
        heading_path=[],
        page_start=None,
        page_end=None,
    )


def test_rrf_math() -> None:
    a, b, c = hit("a"), hit("b"), hit("c")
    fused = fuse({"keyword": [a, b], "vector": [b, c]})
    scores = {f.hit.chunk_id: f.score for f in fused}
    assert scores[b.chunk_id] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[a.chunk_id] == pytest.approx(1 / 61)
    assert scores[c.chunk_id] == pytest.approx(1 / 62)
    assert [f.hit.chunk_id for f in fused] == [b.chunk_id, a.chunk_id, c.chunk_id]
    assert fused[0].sources == ("keyword", "vector")


def test_rrf_custom_k_and_repeated_chunk_counts_once() -> None:
    a = hit("a")
    fused = fuse({"keyword": [a, a]}, k=10)
    assert len(fused) == 1
    assert fused[0].score == pytest.approx(1 / 11)


def test_fuse_is_deterministic_with_ties() -> None:
    doc = uuid.UUID(int=1)
    x = hit("x", doc=doc, start=10)
    y = hit("y", doc=doc, start=0)
    # Symmetric ranks: equal scores; ties break by (document_id, char_start, chunk_id).
    fused = fuse({"keyword": [x, y], "vector": [y, x]})
    assert [f.hit.chunk_id for f in fused] == [y.chunk_id, x.chunk_id]
    again = fuse({"vector": [y, x], "keyword": [x, y]})
    assert [f.hit.chunk_id for f in again] == [f.hit.chunk_id for f in fused]


def test_normalize_score_bounds() -> None:
    assert normalize_score(1 / 61, 1) == pytest.approx(1.0)
    assert normalize_score(2 / 61, 2) == pytest.approx(1.0)
    assert 0 < normalize_score(1 / 70, 2) < 1


def test_dedupe_identical_text_and_overlapping_ranges() -> None:
    doc = uuid.uuid4()
    first = Candidate(hit("same text here", doc=doc, start=0), 1.0)
    dup_text = Candidate(hit("Same   TEXT here", doc=uuid.uuid4(), start=500), 0.9)
    overlap = Candidate(hit("different words entirely", doc=doc, start=2, end=20), 0.8)
    adjacent = Candidate(hit("adjacent chunk text goes on", doc=doc, start=12, end=40), 0.7)
    kept = deduplicate([first, dup_text, overlap, adjacent])
    assert kept == [first, adjacent]  # adjacent chunks sharing a little text both survive


def test_diversify_limits_per_document_in_rank_order() -> None:
    doc = uuid.uuid4()
    cands = [Candidate(hit(str(i), doc=doc), 1 - i / 10) for i in range(5)]
    other = Candidate(hit("o"), 0.01)
    kept = diversify([*cands, other], 3)
    assert kept == [*cands[:3], other]
    with pytest.raises(ValueError, match="max_per_document"):
        diversify(cands, 0)


class _Reverse:
    def rerank(self, query: str, candidates: Any) -> list[Candidate]:
        return list(reversed(candidates))


class _Inventor:
    def rerank(self, query: str, candidates: Any) -> list[Candidate]:
        return [*candidates, Candidate(hit("invented"), 9.0)]


def test_reranker_may_reorder_but_not_invent() -> None:
    cands = [Candidate(hit("a"), 1.0), Candidate(hit("b"), 0.5)]
    assert apply_reranker(None, "q", cands) == cands
    assert apply_reranker(_Reverse(), "q", cands) == list(reversed(cands))
    with pytest.raises(ValueError, match="introduce"):
        apply_reranker(_Inventor(), "q", cands)
