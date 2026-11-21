"""ORM mirror of ``prompt_versions`` (migration ``0016_prompt_versions``). RLS, grants and the seed
definer live in the migration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

PROMPT_KINDS: Final = ("chat_system", "graph_extract", "research_plan")
PROMPT_STATES: Final = ("draft", "active", "retired")
BODY_MAX_LENGTH: Final = 6000


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        CheckConstraint(_in("kind", PROMPT_KINDS), name="ck_prompt_versions_kind"),
        CheckConstraint(_in("state", PROMPT_STATES), name="ck_prompt_versions_state"),
        CheckConstraint("version >= 1", name="ck_prompt_versions_version"),
        CheckConstraint(
            f"length(body) BETWEEN 1 AND {BODY_MAX_LENGTH}", name="ck_prompt_versions_body"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_prompt_versions_sha256"),
        Index(
            "uq_prompt_versions_org_kind_version",
            "org_id",
            "kind",
            "version",
            unique=True,
            postgresql_where=text("org_id IS NOT NULL"),
        ),
        Index(
            "uq_prompt_versions_global_kind_version",
            "kind",
            "version",
            unique=True,
            postgresql_where=text("org_id IS NULL"),
        ),
        Index(
            "uq_prompt_versions_org_active",
            "org_id",
            "kind",
            unique=True,
            postgresql_where=text("state = 'active' AND org_id IS NOT NULL"),
        ),
        Index(
            "uq_prompt_versions_global_active",
            "kind",
            unique=True,
            postgresql_where=text("state = 'active' AND org_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        _tstz(), nullable=False, server_default=text("now()")
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    activated_at: Mapped[datetime | None] = mapped_column(_tstz(), nullable=True)
