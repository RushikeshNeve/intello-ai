"""ORM mirror of ``workspace_pii_settings`` (migration ``0027_pii_policy``; RLS lives there)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, ForeignKeyConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _tstz


class WorkspacePiiSettings(Base):
    __tablename__ = "workspace_pii_settings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_workspace_pii_settings_workspace",
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_workspace_pii_settings_org", ondelete="CASCADE"),
    )
    record_counts: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    mask_indexed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_workspace_pii_settings_updated_by")
    )
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
