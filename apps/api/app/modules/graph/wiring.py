"""Lazy per-process dependencies of the graph read routes (override with
``app.dependency_overrides``). This is the ONE place that adapts to the real graph repository:
until it is connected ``get_graph_read_repository`` answers 503 ``graph_unavailable``.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.modules.graph.ports_read import ChunkRef, ChunkSource, GraphReadRepository
from app.modules.graph.read_settings import GraphReadSettings, get_graph_read_settings
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.knowledge.ports import SearchHit
from app.modules.retrieval.citations import ArtifactUnavailable, load_version_chunk
from app.modules.retrieval.scope import RetrievalScope
from app.modules.retrieval.wiring import get_artifact_store

GRAPH_UNAVAILABLE = "graph_unavailable"


def graph_unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, GRAPH_UNAVAILABLE)


@lru_cache(maxsize=1)
def default_graph_read_repository() -> GraphReadRepository:
    from app.infrastructure.rushdb.graph import RushDbGraphRepository
    from app.infrastructure.rushdb.settings import RushDbSettings
    from app.modules.graph.read_adapter import StoreGraphReadRepository

    return StoreGraphReadRepository(RushDbGraphRepository(RushDbSettings()))


def get_graph_read_repository() -> GraphReadRepository:
    try:
        return default_graph_read_repository()
    except Exception:  # noqa: BLE001 - index not configured: the routes answer 503
        raise graph_unavailable() from None


class ArtifactChunkSource:
    """Chunk text from the verified ``chunks.json`` artifact of the evidence's own revision."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    def fetch_chunks(
        self, session: Session, scope: RetrievalScope, refs: Sequence[ChunkRef]
    ) -> list[SearchHit]:
        out: list[SearchHit] = []
        for r in refs:
            try:
                view = load_version_chunk(
                    session,
                    self._artifacts,
                    org_id=scope.org_id,
                    document_id=r.document_id,
                    version_id=r.version_id,
                    chunk_id=r.chunk_id,
                )
            except ArtifactUnavailable:
                continue  # missing/corrupt artifact: that evidence simply does not count
            if view is None:
                continue
            out.append(
                SearchHit(
                    chunk_id=r.chunk_id,
                    document_id=r.document_id,
                    version_id=r.version_id,
                    revision_id=r.revision_id,
                    workspace_id=r.workspace_id,
                    score=0.0,
                    text=view.text,
                    locators=list(view.locators),
                    heading_path=list(view.heading_path),
                    page_start=view.page_start,
                    page_end=view.page_end,
                )
            )
        return out


def get_chunk_source(
    artifacts: Annotated[ArtifactStore, Depends(get_artifact_store)],
) -> ChunkSource:
    return ArtifactChunkSource(artifacts)


GraphRepoDep = Annotated[GraphReadRepository, Depends(get_graph_read_repository)]
ChunkSourceDep = Annotated[ChunkSource, Depends(get_chunk_source)]
GraphSettingsDep = Annotated[GraphReadSettings, Depends(get_graph_read_settings)]
