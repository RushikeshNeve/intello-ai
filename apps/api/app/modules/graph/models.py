"""ORM models of the M05 graph identity/review schema (migration ``0008_graph``).

PostgreSQL is the authority for entity identity and merge review (contract m05 section 1); the
RushDB graph is a projection. Constraint and index names mirror the migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz
from app.modules.graph.ontology import ENTITY_STATES, ENTITY_TYPES

__all__ = ["CANDIDATE_STATES", "GRAPH_TABLES", "EntityMergeCandidate", "GraphEntity"]

GRAPH_TABLES: Final = ("graph_entities", "entity_merge_candidates")
CANDIDATE_STATES: Final = ("pending", "accepted", "rejected")


class GraphEntity(Base):
    __tablename__ = "graph_entities"
    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_graph_entities_id_org_id"),
        UniqueConstraint("id", "workspace_id", name="uq_graph_entities_id_workspace_id"),
        CheckConstraint(_in("type", ENTITY_TYPES), name="ck_graph_entities_type"),
        CheckConstraint(_in("state", ENTITY_STATES), name="ck_graph_entities_state"),
        CheckConstraint(
            "(state = 'merged') = (merged_into IS NOT NULL) AND merged_into IS DISTINCT FROM id",
            name="ck_graph_entities_merged_into",
        ),
        CheckConstraint(
            "length(canonical_name) BETWEEN 1 AND 200", name="ck_graph_entities_canonical_name"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_graph_entities_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["merged_into", "workspace_id"],
            ["graph_entities.id", "graph_entities.workspace_id"],
            name="fk_graph_entities_merged_into",
        ),
        Index("ix_graph_entities_workspace_id_normalized_name", "workspace_id", "normalized_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    merged_into: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        _tstz(), nullable=False, server_default=text("now()")
    )


class EntityMergeCandidate(Base):
    __tablename__ = "entity_merge_candidates"
    __table_args__ = (
        UniqueConstraint("entity_a", "entity_b", name="uq_entity_merge_candidates_pair"),
        UniqueConstraint("id", "workspace_id", name="uq_entity_merge_candidates_id_workspace_id"),
        CheckConstraint("entity_a < entity_b", name="ck_entity_merge_candidates_order"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_entity_merge_candidates_score"),
        CheckConstraint(_in("state", CANDIDATE_STATES), name="ck_entity_merge_candidates_state"),
        CheckConstraint(
            "(state = 'pending') = (decided_at IS NULL)", name="ck_entity_merge_candidates_decided"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_entity_merge_candidates_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["entity_a", "workspace_id"],
            ["graph_entities.id", "graph_entities.workspace_id"],
            name="fk_entity_merge_candidates_entity_a",
        ),
        ForeignKeyConstraint(
            ["entity_b", "workspace_id"],
            ["graph_entities.id", "graph_entities.workspace_id"],
            name="fk_entity_merge_candidates_entity_b",
        ),
        Index("ix_entity_merge_candidates_workspace_state", "workspace_id", "state", "score"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entity_a: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entity_b: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(_tstz())
    created_at: Mapped[datetime] = mapped_column(
        _tstz(), nullable=False, server_default=text("now()")
    )
