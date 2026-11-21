"""The retrieval pipeline and the grouped search result (M04.01.02-.03, search API).

``gather`` needs no database (index + embedder only); ``revalidate`` needs a tenant session.
:func:`retrieve` does both with one session, for callers without an HTTP request (the chat worker).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from sqlalchemy.orm import Session

from app.modules.ai.embeddings import EmbeddingProvider
from app.modules.knowledge.ports import KnowledgeRepository
from app.modules.retrieval import cache as result_cache
from app.modules.retrieval.evidence import Evidence, revalidate
from app.modules.retrieval.hybrid import Mode, retrieve_candidates
from app.modules.retrieval.ranking import (
    Candidate,
    Reranker,
    apply_reranker,
    fuse,
)
from app.modules.retrieval.scope import RetrievalScope
from app.modules.retrieval.snippets import build_snippet, highlight_ranges, query_terms


@dataclass(frozen=True, slots=True)
class Gathered:
    """Fused (and optionally reranked) candidates that are NOT yet revalidated or de-duplicated."""

    candidates: list[Candidate]
    n_lists: int
    degraded: tuple[str, ...]
    candidate_count: int
    max_per_document: int = 3


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: list[Evidence]
    dropped: int
    degraded: tuple[str, ...]
    candidate_count: int


def gather(
    scope: RetrievalScope,
    repo: KnowledgeRepository,
    embedder: EmbeddingProvider | None,
    query: str,
    *,
    candidates: int = 50,
    mode: Mode = "hybrid",
    max_per_document: int = 3,
    reranker: Reranker | None = None,
) -> Gathered:
    key = None
    if reranker is None:  # M11.04 cache (flag off: lookup is a no-op); finish() revalidates hits
        key, cached = result_cache.lookup(
            scope,
            query,
            space=scope.active_space or (embedder.spec.space_id if embedder is not None else "none"),
            mode=mode,
            candidates=candidates,
            max_per_document=max_per_document,
        )
        if isinstance(cached, Gathered):
            return cached
    found = retrieve_candidates(scope, repo, embedder, query, limit=candidates, mode=mode)
    fused = fuse(found.rankings)
    ranked = apply_reranker(reranker, query, fused)
    gathered = Gathered(
        ranked, max(1, len(found.rankings)), found.degraded, len(fused), max_per_document
    )
    result_cache.store(key, gathered)
    return gathered


def finish(
    session: Session,
    scope: RetrievalScope,
    gathered: Gathered,
    *,
    top_k: int,
    content_types: Sequence[str] | None = None,
) -> RetrievalResult:
    evidence, dropped = revalidate(
        session,
        scope,
        gathered.candidates,
        n_lists=gathered.n_lists,
        max_per_document=gathered.max_per_document,
    )
    if content_types:
        wanted = {c.lower() for c in content_types}
        evidence = [e for e in evidence if e.content_type.lower() in wanted]
        evidence = [_renumber(e, n) for n, e in enumerate(evidence, 1)]
    evidence = evidence[:top_k]
    return RetrievalResult(evidence, dropped, gathered.degraded, gathered.candidate_count)


def _renumber(e: Evidence, n: int) -> Evidence:
    return replace(e, evidence_id=f"e{n}")


def retrieve(
    session: Session,
    scope: RetrievalScope,
    repo: KnowledgeRepository,
    embedder: EmbeddingProvider | None,
    query: str,
    *,
    top_k: int = 8,
    candidates: int = 50,
    mode: Mode = "hybrid",
    max_per_document: int = 3,
    content_types: Sequence[str] | None = None,
    reranker: Reranker | None = None,
) -> RetrievalResult:
    """query -> candidates -> fuse -> rerank -> revalidate -> dedupe/diversify -> top-k Evidence."""
    gathered = gather(
        scope,
        repo,
        embedder,
        query,
        candidates=candidates,
        mode=mode,
        max_per_document=max_per_document,
        reranker=reranker,
    )
    return finish(session, scope, gathered, top_k=top_k, content_types=content_types)


# ------------------------------------------------------------------------------- grouping


@dataclass(frozen=True, slots=True)
class SnippetOut:
    evidence_id: str
    text: str
    page_start: int | None
    page_end: int | None
    highlights: list[list[int]]
    chunk_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ResultOut:
    document_id: uuid.UUID
    title: str
    workspace_id: uuid.UUID
    workspace_slug: str
    content_type: str
    version_id: uuid.UUID
    version_number: int
    score: float
    snippets: list[SnippetOut]


def group_results(
    evidence: Sequence[Evidence], query: str, *, limit: int, max_snippets: int = 3
) -> list[ResultOut]:
    """Group by document (best first, at most ``max_snippets`` snippets each)."""
    terms = query_terms(query)
    order: list[uuid.UUID] = []
    groups: dict[uuid.UUID, list[Evidence]] = {}
    for ev in evidence:  # already best first
        if ev.document_id not in groups:
            order.append(ev.document_id)
            groups[ev.document_id] = []
        if len(groups[ev.document_id]) < max_snippets:
            groups[ev.document_id].append(ev)
    results: list[ResultOut] = []
    for doc_id in order[:limit]:
        items = groups[doc_id]
        best = items[0]
        snippets = []
        for ev in items:
            snippet = build_snippet(ev.text, terms)
            snippets.append(
                SnippetOut(
                    ev.evidence_id,
                    snippet,
                    ev.page_start,
                    ev.page_end,
                    highlight_ranges(snippet, terms),
                    ev.chunk_id,
                )
            )
        results.append(
            ResultOut(
                document_id=doc_id,
                title=best.title,
                workspace_id=best.workspace_id,
                workspace_slug=best.workspace_slug,
                content_type=best.content_type,
                version_id=best.version_id,
                version_number=best.document_version_number,
                score=round(best.score, 6),
                snippets=snippets,
            )
        )
    return results


# ------------------------------------------------------------------------------ rate limit

_MAX_BUCKETS: Final = 10_000


class TokenBucketLimiter:
    """In-process token bucket per key: ``rate_per_min`` tokens/minute, burst = ``rate_per_min``.

    The clock is injectable (monotonic seconds). A shared limiter is M08.
    """

    def __init__(self, rate_per_min: int, clock: Callable[[], float] = time.monotonic) -> None:
        if rate_per_min < 1:
            raise ValueError("rate_per_min must be >= 1")
        self._capacity = float(rate_per_min)
        self._rate = rate_per_min / 60.0
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last)

    def acquire(self, key: str) -> float:
        """Take one token. Returns ``0.0`` when allowed, else seconds until one is available."""
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + max(0.0, now - last) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed = 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed = (1.0 - tokens) / self._rate
            if len(self._buckets) > _MAX_BUCKETS:
                self._prune(now)
            return allowed

    def _prune(self, now: float) -> None:
        full = [
            k
            for k, (t, last) in self._buckets.items()
            if t + max(0.0, now - last) * self._rate >= self._capacity
        ]
        for k in full:
            del self._buckets[k]
