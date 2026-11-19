"""Lazy per-process dependencies of the search routes (overridable with
``app.dependency_overrides`` in tests), following ``ingestion/wiring.py``.

Constructing the real index/embedder/artifact store can fail when the environment is not
configured: that is reported as ``search_unavailable`` (503), never a 500.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.modules.ai.embeddings import EmbeddingClient, EmbeddingProvider
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.knowledge.ports import KnowledgeRepository
from app.modules.retrieval.service import TokenBucketLimiter
from app.modules.retrieval.settings import SearchSettings, get_search_settings

SEARCH_UNAVAILABLE = "search_unavailable"


def search_unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, SEARCH_UNAVAILABLE)


def get_knowledge_repository() -> KnowledgeRepository:
    from app.modules.ingestion import wiring

    try:
        return wiring.default_repository()
    except Exception:  # noqa: BLE001 - misconfiguration must not surface as a 500 with details
        raise search_unavailable() from None


@lru_cache(maxsize=1)
def _embedder() -> EmbeddingProvider:
    return EmbeddingClient.from_settings()


def get_embedder() -> EmbeddingProvider | None:
    """``None`` when the embedding client cannot be built: the search degrades to keyword."""
    try:
        return _embedder()
    except Exception:  # noqa: BLE001
        return None


def get_artifact_store() -> ArtifactStore:
    from app.modules.ingestion import wiring

    try:
        return wiring.default_artifact_store()
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage_unavailable") from None


@lru_cache(maxsize=1)
def _limiter() -> TokenBucketLimiter:
    return TokenBucketLimiter(get_search_settings().SEARCH_RATE_PER_MIN)


def get_search_limiter() -> TokenBucketLimiter:
    return _limiter()


RepoDep = Annotated[KnowledgeRepository, Depends(get_knowledge_repository)]
EmbedderDep = Annotated[EmbeddingProvider | None, Depends(get_embedder)]
ArtifactsDep = Annotated[ArtifactStore, Depends(get_artifact_store)]
LimiterDep = Annotated[TokenBucketLimiter, Depends(get_search_limiter)]
SettingsDep = Annotated[SearchSettings, Depends(get_search_settings)]
