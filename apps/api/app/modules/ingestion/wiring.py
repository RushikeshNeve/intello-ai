"""Lazy default dependencies of the pipeline and the deletion handler (built from the environment).

Everything is imported and constructed on first use, so importing the pipeline never needs a
database, an object store or an index, and tests inject fakes through the constructors instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import create_db_engine, tenant_session
from app.modules.ai.embeddings import EmbeddingClient, EmbeddingProvider
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.ingestion.runner_types import WorkerSessions
from app.modules.knowledge.ports import KnowledgeRepository
from app.modules.storage.ports import ObjectStore


@lru_cache(maxsize=1)
def _worker_engine() -> Engine:
    url = get_settings().sqlalchemy_worker_url
    if url is None:
        raise RuntimeError("DATABASE_WORKER_URL is not configured")
    return create_db_engine(url)


def default_worker_sessions() -> WorkerSessions:
    """Worker-role transactions with ``app.current_org_id`` set (RLS binds every statement)."""

    @contextmanager
    def open_session(*, org_id: uuid.UUID) -> Iterator[Session]:
        with tenant_session(user_id=None, org_id=org_id, engine=_worker_engine()) as session:
            yield session

    return open_session


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
