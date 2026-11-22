"""Authorized retrieval, ranking, citations and the search API (M04.01).

Callers without an HTTP request (the chat worker): ``resolve_scope`` (app-role session) ->
``retrieve`` (or ``gather`` + ``finish``) -> ``Evidence`` -> ``resolve_citation``.
"""

from app.modules.retrieval.citations import (
    ArtifactUnavailable,
    ChunkView,
    CitationRef,
    ResolvedCitation,
    load_version_chunk,
    resolve_citation,
)
from app.modules.retrieval.evidence import Evidence, revalidate
from app.modules.retrieval.hybrid import (
    CandidateSet,
    EmbeddingSpaceMismatch,
    SearchUnavailable,
    retrieve_candidates,
)
from app.modules.retrieval.ranking import (
    Candidate,
    Reranker,
    deduplicate,
    diversify,
    fuse,
)
from app.modules.retrieval.scope import EmptyScope, RetrievalScope, resolve_scope
from app.modules.retrieval.service import (
    Gathered,
    RetrievalResult,
    TokenBucketLimiter,
    finish,
    gather,
    group_results,
    retrieve,
)

__all__ = [
    "ArtifactUnavailable",
    "Candidate",
    "CandidateSet",
    "ChunkView",
    "CitationRef",
    "EmbeddingSpaceMismatch",
    "EmptyScope",
    "Evidence",
    "Gathered",
    "Reranker",
    "ResolvedCitation",
    "RetrievalResult",
    "RetrievalScope",
    "SearchUnavailable",
    "TokenBucketLimiter",
    "deduplicate",
    "diversify",
    "finish",
    "fuse",
    "gather",
    "group_results",
    "load_version_chunk",
    "resolve_citation",
    "resolve_scope",
    "retrieve",
    "retrieve_candidates",
    "revalidate",
]
