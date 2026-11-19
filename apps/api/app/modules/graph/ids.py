"""Deterministic graph ids (contract m05 section 2): re-extraction merges, never duplicates."""

from __future__ import annotations

import uuid
from typing import Final

from app.modules.graph.ontology import normalize_name

GRAPH_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "https://intello.ai/graph")


def entity_id_for(
    org_id: uuid.UUID, workspace_id: uuid.UUID, entity_type: str, name: str
) -> uuid.UUID:
    """uuid5 over org/workspace/type/normalized name (same name, other type = other entity)."""
    return uuid.uuid5(
        GRAPH_NAMESPACE, f"entity:{org_id}:{workspace_id}:{entity_type}:{normalize_name(name)}"
    )


def claim_id_for(
    subject_id: uuid.UUID,
    predicate: str,
    object_key: str,
    revision_id: uuid.UUID,
) -> uuid.UUID:
    """``object_key`` is the object entity id (str) or ``text:<normalized text>``."""
    return uuid.uuid5(GRAPH_NAMESPACE, f"claim:{subject_id}:{predicate}:{object_key}:{revision_id}")


def evidence_id_for(claim_id: uuid.UUID, chunk_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(GRAPH_NAMESPACE, f"evidence:{claim_id}:{chunk_id}")
