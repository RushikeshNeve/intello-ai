"""ORM mirror of ``memories`` (migration ``0010_memory_research``). RLS, grants and the definer
functions live in the migration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

VISIBILITIES: Final = ("private", "workspace")
KINDS: Final = ("fact", "preference", "decision")
STATES: Final = ("suggested", "approved", "rejected", "expired", "invalidated")
SOURCES: Final = ("user", "chat", "research")
INVALIDATION_REASONS: Final = ("evidence_deleted", "source_revoked", "evidence_superseded")
STATEMENT_MAX_LENGTH: Final = 2000

STATE_SUGGESTED: Final = "suggested"
STATE_APPROVED: Final = "approved"
STATE_REJECTED: Final = "rejected"
STATE_EXPIRED: Final = "expired"
STATE_INVALIDATED: Final = "invalidated"


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(_in("visibility", VISIBILITIES), name="ck_memories_visibility"),
        CheckConstraint(_in("kind", KINDS), name="ck_memories_kind"),
        CheckConstraint(_in("state", STATES), name="ck_memories_state"),
        CheckConstraint(_in("source", SOURCES), name="ck_memories_source"),
        CheckConstraint(
            f"length(statement) BETWEEN 1 AND {STATEMENT_MAX_LENGTH}", name="ck_memories_statement"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_memories_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_memories_org_id_owner_id_created_at", "org_id", "owner_id", "created_at", "id"),
        Index(
            "ix_memories_workspace_id",
            "workspace_id",
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "ix_memories_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL AND state IN ('suggested', 'approved')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_memories_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_memories_owner_id")
    )
    visibility: Mapped[str] = mapped_column(Text, server_default="private")
    kind: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="suggested")
    evidence: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    source: Mapped[str] = mapped_column(Text, server_default="user")
    source_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(_tstz())
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_memories_approved_by")
    )
    approved_at: Mapped[datetime | None] = mapped_column(_tstz())
    invalidated_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    deleted_at: Mapped[datetime | None] = mapped_column(_tstz())
