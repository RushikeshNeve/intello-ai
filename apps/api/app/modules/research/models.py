"""ORM mirror of the research tables (migration ``0010_memory_research``). RLS, grants and the
definer functions (claim / reap / cancel) live in the migration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

STATES: Final = ("planned", "running", "completed", "failed", "cancelled")
STATE_PLANNED: Final = "planned"
STATE_RUNNING: Final = "running"
STATE_COMPLETED: Final = "completed"
STATE_FAILED: Final = "failed"
STATE_CANCELLED: Final = "cancelled"
ACTIVE_STATES: Final = (STATE_PLANNED, STATE_RUNNING)
TERMINAL_STATES: Final = (STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED)

STEP_KINDS: Final = ("plan", "search", "synthesize", "report")
STEP_STATES: Final = ("pending", "running", "done", "failed")
QUESTION_MAX_LENGTH: Final = 2000

AGGREGATE_RESEARCH_SESSION: Final = "research_session"
EVENT_RESEARCH_SESSION_QUEUED: Final = "research.session.queued"


class ResearchSession(Base):
    __tablename__ = "research_sessions"
    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_research_sessions_id_org_id"),
        CheckConstraint(_in("state", STATES), name="ck_research_sessions_state"),
        CheckConstraint(
            f"length(question) BETWEEN 1 AND {QUESTION_MAX_LENGTH}",
            name="ck_research_sessions_question",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_research_sessions_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "ix_research_sessions_org_id_created_by_created_at",
            "org_id",
            "created_by",
            "created_at",
            "id",
        ),
        Index(
            "ix_research_sessions_running_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_research_sessions_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_research_sessions_created_by")
    )
    question: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="planned")
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    scope_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(_tstz())
    lease_epoch: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(_tstz())
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())


class ResearchStep(Base):
    __tablename__ = "research_steps"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_research_steps_session_id_seq"),
        CheckConstraint(_in("kind", STEP_KINDS), name="ck_research_steps_kind"),
        CheckConstraint(_in("state", STEP_STATES), name="ck_research_steps_state"),
        ForeignKeyConstraint(
            ["session_id", "org_id"],
            ["research_sessions.id", "research_sessions.org_id"],
            name="fk_research_steps_session",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_research_steps_org_id", ondelete="CASCADE"),
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    query: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="pending")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime | None] = mapped_column(_tstz())
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())


class ResearchReport(Base):
    __tablename__ = "research_reports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "org_id"],
            ["research_sessions.id", "research_sessions.org_id"],
            name="fk_research_reports_session",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_research_reports_org_id", ondelete="CASCADE"),
    )
    sections: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
