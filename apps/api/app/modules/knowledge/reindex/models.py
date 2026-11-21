"""ORM mirror of ``reindex_jobs`` / ``org_active_spaces`` (migration ``0018_reindex``).

RLS, grants and the definer functions live in the migration; the service talks SQL directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

REINDEX_STATES: Final = (
    "planned",
    "running",
    "verifying",
    "ready",
    "cutover",
    "rolled_back",
    "failed",
)
REINDEX_TABLES: Final = ("reindex_jobs", "org_active_spaces")


class ReindexJob(Base):
    __tablename__ = "reindex_jobs"
    __table_args__ = (
        CheckConstraint(_in("state", REINDEX_STATES), name="ck_reindex_jobs_state"),
        CheckConstraint("from_space <> to_space", name="ck_reindex_jobs_spaces"),
        CheckConstraint("total >= 0 AND done >= 0 AND failed >= 0", name="ck_reindex_jobs_counts"),
        CheckConstraint("to_dimensions >= 1", name="ck_reindex_jobs_dimensions"),
        Index(
            "uq_reindex_jobs_one_active",
            "org_id",
            unique=True,
            postgresql_where=text("state IN ('planned', 'running', 'verifying', 'ready')"),
        ),
        Index("ix_reindex_jobs_org_created", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_reindex_jobs_org_id", ondelete="CASCADE"),
    )
    from_space: Mapped[str] = mapped_column(Text)
    to_space: Mapped[str] = mapped_column(Text)
    to_dimensions: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text, server_default="planned")
    total: Mapped[int] = mapped_column(Integer, server_default="0")
    done: Mapped[int] = mapped_column(Integer, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, server_default="0")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(_tstz())
    lease_epoch: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(Text)
    old_space_purged_at: Mapped[datetime | None] = mapped_column(_tstz())
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(_tstz())
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())


class OrgActiveSpace(Base):
    __tablename__ = "org_active_spaces"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_org_active_spaces_org_id", ondelete="CASCADE"),
        primary_key=True,
    )
    space_id: Mapped[str] = mapped_column(Text)
    reindex_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
