"""Fusion, reranking and evidence de-duplication (M04.01.03). Pure and deterministic.

Ties are broken by ``(document_id, char_start, chunk_id)``: ``SearchHit`` carries no
``chunk_index``, and the first locator's ``char_start`` orders the chunks of a document.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.modules.knowledge.ports import SearchHit

RRF_K: Final = 60
_WS: Final = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Candidate:
    hit: SearchHit
    score: float
    sources: tuple[str, ...] = ()


class Reranker(Protocol):
    """Optional second-stage ranker. May reorder or drop candidates, never invent them."""

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[Candidate]: ...


def char_range(hit: SearchHit) -> tuple[int, int]:
    """Character range of the chunk in the document stream (0,0 when there are no locators)."""
    starts = [int(loc["char_start"]) for loc in hit.locators if "char_start" in loc]
    ends = [int(loc["char_end"]) for loc in hit.locators if "char_end" in loc]
    if not starts or not ends:
        return (0, 0)
    return (min(starts), max(ends))


def position_key(hit: SearchHit) -> tuple[str, int, str]:
    return (str(hit.document_id), char_range(hit)[0], str(hit.chunk_id))


def _sort_key(c: Candidate) -> tuple[float, str, int, str]:
    return (-round(c.score, 12), *position_key(c.hit))


def fuse(rankings: Mapping[str, Sequence[SearchHit]], k: int = RRF_K) -> list[Candidate]:
    """Reciprocal rank fusion: ``score = sum over lists of 1 / (k + rank)`` (rank is 1-based).

    A chunk repeated inside one list counts once (its best rank). The result is sorted by score,
    then by the deterministic position key.
    """
    scores: dict[object, float] = {}
    hits: dict[object, SearchHit] = {}
    sources: dict[object, list[str]] = {}
    for name in sorted(rankings):
        seen: set[object] = set()
        rank = 0
        for hit in rankings[name]:
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            rank += 1
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            hits.setdefault(hit.chunk_id, hit)
            sources.setdefault(hit.chunk_id, []).append(name)
    fused = [Candidate(hits[cid], score, tuple(sources[cid])) for cid, score in scores.items()]
    fused.sort(key=_sort_key)
    return fused


def normalize_score(raw: float, n_lists: int, k: int = RRF_K) -> float:
    """Scale an RRF score to ``(0, 1]`` (1 = rank 1 in every list)."""
    best = max(1, n_lists) / (k + 1)
    return min(1.0, raw / best)


def apply_reranker(
    reranker: Reranker | None, query: str, candidates: Sequence[Candidate]
) -> list[Candidate]:
    """Run ``reranker``; its output must be a sub-multiset of the input (no invented hits)."""
    if reranker is None:
        return list(candidates)
    allowed = {c.hit.chunk_id for c in candidates}
    out = reranker.rerank(query, list(candidates))
    seen: set[object] = set()
    result: list[Candidate] = []
    for c in out:
        if c.hit.chunk_id not in allowed:
            raise ValueError("a reranker may not introduce candidates")
        if c.hit.chunk_id in seen:
            continue
        seen.add(c.hit.chunk_id)
        result.append(c)
    return result


def content_fingerprint(text: str) -> str:
    normalized = _WS.sub(" ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _overlap_ratio(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    smaller = min(a[1] - a[0], b[1] - b[0])
    if inter <= 0 or smaller <= 0:
        return 0.0
    return inter / smaller


def deduplicate(candidates: Sequence[Candidate], *, overlap_ratio: float = 0.5) -> list[Candidate]:
    """Drop near-duplicates, keeping the better-ranked one (input order = rank order).

    Duplicate = identical normalized text, or same document with character ranges overlapping by
    at least ``overlap_ratio`` of the shorter chunk (adjacent chunks share only a small overlap
    and are both kept).
    """
    kept: list[Candidate] = []
    fingerprints: set[str] = set()
    ranges: dict[object, list[tuple[int, int]]] = {}
    for cand in candidates:
        fp = content_fingerprint(cand.hit.text)
        if fp in fingerprints:
            continue
        rng = char_range(cand.hit)
        existing = ranges.get(cand.hit.document_id, [])
        if rng != (0, 0) and any(_overlap_ratio(rng, other) >= overlap_ratio for other in existing):
            continue
        fingerprints.add(fp)
        ranges.setdefault(cand.hit.document_id, []).append(rng)
        kept.append(cand)
    return kept


def diversify(candidates: Sequence[Candidate], max_per_document: int = 3) -> list[Candidate]:
    """Keep at most ``max_per_document`` chunks per document, preserving rank order."""
    if max_per_document < 1:
        raise ValueError("max_per_document must be >= 1")
    counts: dict[object, int] = {}
    kept: list[Candidate] = []
    for cand in candidates:
        n = counts.get(cand.hit.document_id, 0)
        if n >= max_per_document:
            continue
        counts[cand.hit.document_id] = n + 1
        kept.append(cand)
    return kept


def postprocess(candidates: Sequence[Candidate], max_per_document: int = 3) -> list[Candidate]:
    """De-duplicate, then diversify (run AFTER revalidation so a stale revision's chunk can never
    shadow the live one)."""
    return diversify(deduplicate(candidates), max_per_document)
