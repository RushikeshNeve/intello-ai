"""Pure evaluation metrics (M04.03.04/05). No I/O, no ``app`` imports; unit tested.

Retrieval metrics work on a ranked list of *document ids* (first occurrence of each document wins;
a chunk of an already-listed document does not add a rank). Relevance is binary.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Case-folded, whitespace-collapsed text for substring checks."""
    return _SPACE.sub(" ", text).strip().casefold()


def contains(haystack: str, needle: str) -> bool:
    return normalize(needle) in normalize(haystack)


def unique_in_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def recall_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Share of the relevant documents found in the top ``k`` (0.0 without relevant docs)."""
    rel = set(relevant)
    if not rel or k < 1:
        return 0.0
    return len(rel & set(ranked[:k])) / len(rel)


def hit_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """1.0 when at least one relevant document is in the top ``k``."""
    return 1.0 if set(relevant) & set(ranked[:k]) else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: Iterable[str]) -> float:
    rel = set(relevant)
    for position, doc in enumerate(ranked, start=1):
        if doc in rel:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Binary-relevance nDCG: DCG = sum 1/log2(rank+1); normalised by the ideal ordering."""
    rel = set(relevant)
    if not rel or k < 1:
        return 0.0
    dcg = sum(1.0 / math.log2(i + 2) for i, doc in enumerate(ranked[:k]) if doc in rel)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(rel), k)))
    return dcg / ideal


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def rate(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    """``numerator / denominator``; ``empty`` when there is nothing to measure."""
    return numerator / denominator if denominator else empty


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in [0, 100]); 0.0 for no values."""
    if not values:
        return 0.0
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def latency_summary(values_ms: Sequence[float]) -> dict[str, float]:
    return {
        "p50": percentile(values_ms, 50),
        "p95": percentile(values_ms, 95),
        "p99": percentile(values_ms, 99),
        "max": max(values_ms) if values_ms else 0.0,
    }


def abstention_scores(expected: Sequence[bool], actual: Sequence[bool]) -> dict[str, float]:
    """Precision and recall of *abstaining* (True = abstained / should abstain).

    precision = abstained correctly / all abstained (1.0 when it never abstained);
    recall    = abstained correctly / all that should abstain (1.0 when none should).
    """
    if len(expected) != len(actual):
        raise ValueError("expected and actual must have the same length")
    both = sum(1 for e, a in zip(expected, actual, strict=True) if e and a)
    return {
        "precision": rate(both, sum(1 for a in actual if a)),
        "recall": rate(both, sum(1 for e in expected if e)),
    }
