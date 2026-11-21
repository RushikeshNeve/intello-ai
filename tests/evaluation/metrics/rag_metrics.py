"""RAG quality metrics (M12.02): pure functions, deterministic, no model.

This directory deliberately has no ``__init__.py``: ``tests/evaluation/metrics.py`` (the IR helpers)
already owns the name ``metrics``. Import as ``import rag_metrics`` after putting this directory on
``sys.path`` (``report.py`` and the unit tests do that).

Faithfulness and citation support use a lexical overlap check. A model judge can be plugged in later
through the ``Judge`` protocol (``supports(claim, evidence) -> bool``); it is never required.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

_WORD = re.compile(r"[^\W_]+")
_MARKER = re.compile(r"\[(\d+)\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?!\[\d+\])|(?<=\])\s+|\n+")
_STOP = frozenset(
    "a an and are as at be by can do does for from how i in is it its me my of on or our the their "
    "there this to was we what when where which who whom why will with you your".split()
)
SUPPORT_THRESHOLD = 0.6


class Judge(Protocol):
    """Optional pluggable judge (later task): True when ``evidence`` supports ``claim``."""

    def supports(self, claim: str, evidence: str) -> bool: ...


def words(text: str) -> set[str]:
    """Content words, lower-cased, light plural stemming (as in the baseline adapters)."""
    ws = {w for w in _WORD.findall(text.lower()) if w not in _STOP}
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in ws}


def lexical_support(claim: str, evidence: str, threshold: float = SUPPORT_THRESHOLD) -> bool:
    """A claim is supported when >= ``threshold`` of its content words occur in the evidence."""
    cw = words(claim)
    if not cw:
        return False
    return len(cw & words(evidence)) / len(cw) >= threshold


def _ratio(num: int, den: int, empty: float | None = 1.0) -> float | None:
    return empty if den == 0 else num / den


def context_precision(retrieved_docs: Sequence[str], expected_docs: Iterable[str]) -> float | None:
    """Share of retrieved chunks (by their document id) that belong to an expected document.

    None when nothing was retrieved (undefined); callers exclude None from aggregates.
    """
    expected = set(expected_docs)
    return _ratio(sum(1 for d in retrieved_docs if d in expected), len(retrieved_docs), None)


def context_recall(retrieved_docs: Iterable[str], expected_docs: Iterable[str]) -> float | None:
    """Share of expected documents that were retrieved at all. None when none are expected."""
    expected = set(expected_docs)
    got = set(retrieved_docs)
    return _ratio(len(expected & got), len(expected), None)


def split_claims(answer_text: str) -> list[tuple[str, list[str]]]:
    """Split an answer into (claim text without markers, markers like ``"[1]"``) per sentence."""
    out: list[tuple[str, list[str]]] = []
    for part in _SENTENCE.split(answer_text):
        part = part.strip()
        if not part:
            continue
        markers = [f"[{m}]" for m in _MARKER.findall(part)]
        claim = _MARKER.sub("", part).strip()
        if claim:
            out.append((claim, markers))
    return out


def faithfulness(
    answer_text: str,
    citations: Sequence[Any],
    chunk_text: Mapping[str, str],
    *,
    abstained: bool = False,
    judge: Judge | None = None,
) -> float | None:
    """Share of claims supported by the text of a chunk they cite.

    ``citations`` items need ``.marker`` and ``.chunk_id``; ``chunk_text`` maps chunk id to text.
    A claim without a marker, or whose cited chunks do not support it, is unsupported.
    None for an abstention (nothing to be faithful about).
    """
    if abstained:
        return None
    claims = split_claims(answer_text)
    if not claims:
        return 0.0
    by_marker = {c.marker: c.chunk_id for c in citations}
    supported = 0
    for claim, markers in claims:
        evidence = [chunk_text[by_marker[m]] for m in markers if by_marker.get(m) in chunk_text]
        if any(
            judge.supports(claim, e) if judge is not None else lexical_support(claim, e)
            for e in evidence
        ):
            supported += 1
    return supported / len(claims)


def answer_relevance(question: str, answer_text: str, *, abstained: bool = False) -> float | None:
    """Deterministic proxy: share of the question's content words covered by the answer text."""
    if abstained:
        return None
    qw = words(question)
    if not qw:
        return None
    return len(qw & words(_MARKER.sub("", answer_text))) / len(qw)


def citation_validity(
    answer_text: str,
    citations: Sequence[Any],
    registry: Mapping[str, Any],
    *,
    abstained: bool = False,
    judge: Judge | None = None,
) -> tuple[int, int]:
    """(valid, total) citations. Valid = the chunk exists, its document is live (state
    ``published``), and the claim carrying the citation's marker is supported by the chunk text.

    ``registry`` maps chunk id to an object with ``.state`` and ``.text``. An answered answer with
    no citations counts as one invalid citation; an abstention has none.
    """
    if abstained:
        return 0, 0
    if not citations:
        return 0, 1
    claim_of: dict[str, str] = {}
    for claim, markers in split_claims(answer_text):
        for m in markers:
            claim_of.setdefault(m, claim)
    valid = 0
    for cit in citations:
        info = registry.get(cit.chunk_id)
        if info is None or info.state != "published":
            continue
        claim = claim_of.get(cit.marker, "")
        ok = (
            judge.supports(claim, info.text)
            if judge is not None
            else bool(claim) and lexical_support(claim, info.text)
        )
        valid += 1 if ok else 0
    return valid, len(citations)


def abstention_correct(expect_abstain: bool, abstained: bool) -> bool:
    """Unanswerable questions must abstain; answerable ones must not."""
    return expect_abstain == abstained


def mean_defined(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else None
