"""Deterministic identifiers of the ingestion pipeline (M03 contract §2 and §7).

Everything here is pure and dependency-free so the migration, the job code, the processors, the
RushDB adapter and the embedding adapter all derive *exactly* the same ids. Changing any formula or
constant changes every derived id: bump ``PIPELINE_VERSION`` instead of editing the formulas.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Final

# Bump when extraction/chunking semantics change: every document then gets a new revision.
PIPELINE_VERSION: Final = "1"

INGEST_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "https://intello.ai/ingest")


def revision_id_for(
    version_id: uuid.UUID, pipeline_version: str, embedding_space_id: str
) -> uuid.UUID:
    """The knowledge-revision id of one document version under one pipeline and embedding space.

    The same inputs always give the same id, so re-running a job, re-delivering a message or
    re-creating a job for the same version reuses the same revision (idempotent), while a new
    pipeline or embedding space gives a new one.
    """
    return uuid.uuid5(INGEST_NAMESPACE, f"{version_id}:{pipeline_version}:{embedding_space_id}")


def chunk_id_for(revision_id: uuid.UUID, chunk_index: int) -> uuid.UUID:
    """The id of chunk ``chunk_index`` of a revision (also its RushDB record id: writes merge)."""
    if chunk_index < 0:
        raise ValueError("chunk_index must be >= 0")
    return uuid.uuid5(revision_id, f"chunk:{chunk_index}")


def compute_embedding_space_id(
    model_id: str,
    dimensions: int,
    query_prefix: str,
    document_prefix: str,
    spec_version: int,
) -> str:
    """Identify an embedding space (ADR-020): vectors are only comparable inside one space.

    Changing the model, the dimensions, either prefix or the spec version is a NEW space, which
    means a new revision, a new vector index and re-embedding.
    """
    if dimensions <= 0:
        raise ValueError("dimensions must be positive")
    raw = f"{model_id}|{dimensions}|{query_prefix}|{document_prefix}|{spec_version}"
    return "e" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
