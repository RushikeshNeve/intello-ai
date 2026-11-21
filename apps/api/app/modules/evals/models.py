"""ORM mirror of the eval tables (migration ``0017_evals``). RLS and grants live in the migration;
the module reads and writes them with SQL (see ``service.py``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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

from app.core.models import Base, _tstz


def _org_fk(name: str) -> Any:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", name=name, ondelete="CASCADE")
    )


def _scoped_fk(cols: str, target: str, name: str) -> ForeignKeyConstraint:
    """Composite ``(x_id, org_id, workspace_id)`` FK onto ``target``'s scope unique key."""
    return ForeignKeyConstraint(
        [cols, "org_id", "workspace_id"],
        [f"{target}.id", f"{target}.org_id", f"{target}.workspace_id"],
        name=name,
        ondelete="CASCADE",
    )


class EvalSet(Base):
    __tablename__ = "eval_sets"
    __table_args__ = (
        UniqueConstraint("id", "org_id", "workspace_id", name="uq_eval_sets_scope"),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_eval_sets_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_eval_sets_workspace_id", "workspace_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = _org_fk("fk_eval_sets_org_id")
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_eval_sets_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))


class EvalCase(Base):
    __tablename__ = "eval_cases"
    __table_args__ = (
        UniqueConstraint("id", "org_id", "workspace_id", name="uq_eval_cases_scope"),
        _scoped_fk("set_id", "eval_sets", "fk_eval_cases_set"),
        Index("ix_eval_cases_set_id", "set_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    question: Mapped[str] = mapped_column(Text)
    expected_document_ids: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    expected_chunk_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    should_abstain: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        UniqueConstraint("id", "org_id", "workspace_id", name="uq_eval_runs_scope"),
        _scoped_fk("set_id", "eval_sets", "fk_eval_runs_set"),
        Index("ix_eval_runs_set_id", "set_id", "started_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    set_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text, server_default="completed")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_eval_runs_created_by")
    )
    started_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())


class EvalRunResult(Base):
    __tablename__ = "eval_run_results"
    __table_args__ = (
        _scoped_fk("run_id", "eval_runs", "fk_eval_run_results_run"),
        Index("ix_eval_run_results_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    question: Mapped[str] = mapped_column(Text)
    retrieved: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    answer_status: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
