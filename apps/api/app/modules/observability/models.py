"""ORM mirror of ``ai_run_captures`` (migration ``0020_ai_run_captures``, M13.02). RLS, grants and
the sweep function live in the migration; the module reads and writes with SQL (``capture.py``)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _tstz


class AiRunCapture(Base):
    __tablename__ = "ai_run_captures"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", name="uq_ai_run_captures_run_kind"),
        CheckConstraint("kind IN ('chat')", name="ck_ai_run_captures_kind"),
        CheckConstraint("mode IN ('redacted', 'full_admin_only')", name="ck_ai_run_captures_mode"),
        CheckConstraint("length(messages) <= 32000", name="ck_ai_run_captures_messages"),
        CheckConstraint("length(answer) <= 16000", name="ck_ai_run_captures_answer"),
        Index("ix_ai_run_captures_org_created", "org_id", text("created_at DESC")),
        Index("ix_ai_run_captures_expires", "expires_at"),
        Index("ix_ai_run_captures_documents", "document_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_runs.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'chat'"))
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    messages: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        _tstz(), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(_tstz(), nullable=False)
