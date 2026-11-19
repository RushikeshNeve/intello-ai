"""Lazy default dependencies of the pipeline and the deletion handler (built from the environment).

Everything is imported and constructed on first use, so importing the pipeline never needs a
database, an object store or an index, and tests inject fakes through the constructors instead.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.modules.ai.embeddings import EmbeddingClient, EmbeddingProvider
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.ingestion.runner_types import WorkerSessions
from app.modules.knowledge.ports import KnowledgeRepository
from app.modules.storage.ports import ObjectStore


def default_worker_sessions(settings: Settings | None = None) -> WorkerSessions:
    """Worker-role transactions with ``app.current_org_id`` set (RLS binds every statement)."""
    from app.workers.worker_db import get_worker_engine, session_factory_for

    return session_factory_for(get_worker_engine(settings or get_settings()))


@lru_cache(maxsize=1)
def default_repository() -> KnowledgeRepository:
    from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
    from app.infrastructure.rushdb.settings import RushDbSettings

    return RushDbKnowledgeRepository(RushDbSettings())


def _s3(bucket: str | None) -> ObjectStore:
    from app.infrastructure.object_storage.s3 import S3ObjectStore
    from app.infrastructure.object_storage.settings import ObjectStoreSettings

    settings = ObjectStoreSettings()
    return S3ObjectStore(settings, bucket=bucket)


@lru_cache(maxsize=1)
def default_uploads_store() -> ObjectStore:
    """The bucket of original uploads (``S3_BUCKET_ORIGINALS``)."""
    return _s3(None)


@lru_cache(maxsize=1)
def default_artifact_store() -> ArtifactStore:
    """The artifacts bucket (``S3_BUCKET_ARTIFACTS``)."""
    from app.infrastructure.object_storage.settings import ObjectStoreSettings

    return ArtifactStore(_s3(ObjectStoreSettings().S3_BUCKET_ARTIFACTS))


def default_embedder() -> EmbeddingProvider:
    return EmbeddingClient.from_settings()
