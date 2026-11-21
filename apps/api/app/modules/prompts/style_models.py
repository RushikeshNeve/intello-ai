"""ORM mirror of ``workspace_styles`` (migration ``0019b_workspace_styles``). RLS and grants live in
the migration; ``styles.py`` reads and writes the table with SQL."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _tstz


class WorkspaceStyle(Base):
    __tablename__ = "workspace_styles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_workspace_styles_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "uq_workspace_styles_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "uq_workspace_styles_org_default",
            "org_id",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_workspace_styles_org", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tone: Mapped[str] = mapped_column(Text, server_default=text("'neutral'"))
    answer_format: Mapped[str] = mapped_column(Text, server_default=text("'concise'"))
    language: Mapped[str | None] = mapped_column(Text)
    citation_style: Mapped[str] = mapped_column(Text, server_default=text("'numeric'"))
    custom_instructions: Mapped[str] = mapped_column(Text, server_default=text("''"))
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_workspace_styles_updated_by")
    )
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
