"""Candidate retrieval (M04.01.02): keyword and vector legs under the authorized scope.

Only ``KnowledgeRepository.search_*`` is used, always with ``scope.filters()`` (the adapter adds the
workspace predicate and ``published = true``; PostgreSQL revalidation happens later).

Degrade rules: the embedder failing (or answering with a vector the index cannot take) degrades
the mode to keyword and reports ``embedding_unavailable``; an unreachable index raises
:class:`SearchUnavailable` (HTTP 503). An infrastructure failure is never an empty result.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from app.modules.ai.embeddings import EmbeddingError, EmbeddingProvider
from app.modules.knowledge.ports import (
    KnowledgeRepository,
    KnowledgeStoreError,
    KnowledgeStoreUnavailable,
    SearchHit,
    validate_query_text,
)
from app.modules.retrieval.scope import RetrievalScope

Mode = Literal["hybrid", "keyword", "vector"]
DEGRADED_EMBEDDING: Final = "embedding_unavailable"
# RushDB answers a tenant that never indexed anything with `not_found` (observed on 3.0.10).
_NOTHING_INDEXED: Final = frozenset({"index_missing", "not_found"})
_WS: Final = re.compile(r"\s+")


class SearchUnavailable(Exception):
    """The knowledge index is unreachable or failing (answer 503 ``search_unavailable``)."""


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """Named rankings (best first) and what was degraded."""

    rankings: dict[str, list[SearchHit]]
    degraded: tuple[str, ...] = ()
    mode: Mode = "hybrid"


def normalize_query(query: str) -> str:
    return validate_query_text(_WS.sub(" ", query))


def retrieve_candidates(
    scope: RetrievalScope,
    repo: KnowledgeRepository,
    embedder: EmbeddingProvider | None,
    query: str,
    *,
    limit: int = 50,
    mode: Mode = "hybrid",
) -> CandidateSet:
    text = normalize_query(query)
    degraded: list[str] = []
    rankings: dict[str, list[SearchHit]] = {}
    effective: Mode = mode
    vector: list[float] | None = None
    space: str | None = None

    if mode in ("hybrid", "vector"):
        if embedder is None:
            degraded.append(DEGRADED_EMBEDDING)
        else:
            try:
                vector = embedder.embed_query(text)
                space = embedder.spec.space_id
            except EmbeddingError:
                degraded.append(DEGRADED_EMBEDDING)
        if vector is None:
            effective = "keyword"

    try:
        if effective in ("hybrid", "keyword"):
            rankings["keyword"] = _call(
                lambda: repo.search_keyword(scope.org, text, scope.filters(), limit=limit)
            )
        if vector is not None and space is not None and effective in ("hybrid", "vector"):
            vec, sp = vector, space
            try:
                rankings["vector"] = _call(
                    lambda: repo.search_vector(scope.org, sp, vec, scope.filters(), limit=limit)
                )
            except ValueError:  # vector length does not fit the index: embedder/index mismatch
                degraded.append(DEGRADED_EMBEDDING)
                if effective == "vector":
                    effective = "keyword"
                    rankings["keyword"] = _call(
                        lambda: repo.search_keyword(scope.org, text, scope.filters(), limit=limit)
                    )
    except KnowledgeStoreUnavailable:
        raise SearchUnavailable from None
    return CandidateSet(rankings, tuple(dict.fromkeys(degraded)), effective)


def _call(fn: Callable[[], list[SearchHit]]) -> list[SearchHit]:
    """Run one leg. A missing index means nothing is indexed yet (empty), any other store failure
    is :class:`SearchUnavailable`."""
    try:
        result = fn()
    except KnowledgeStoreUnavailable:
        raise
    except KnowledgeStoreError as exc:
        if exc.code in _NOTHING_INDEXED:
            return []
        raise KnowledgeStoreUnavailable("index failure", code=exc.code) from None
    return result
