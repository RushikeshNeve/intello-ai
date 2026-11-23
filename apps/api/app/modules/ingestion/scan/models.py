"""ORM mirror of migration ``0024_document_findings`` (M14.02; RLS, grants and the release definer
live in the migration). The scan service writes through SQL; these classes exist so the metadata,
the RLS registries and ``compare_metadata`` cover the three tables."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _tstz


class DocumentFinding(Base):
    __tablename__ = "document_findings"
    __table_args__ = (
        UniqueConstraint(
            "version_id", "rule_id", "location", name="uq_document_findings_rule_location"
        ),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high')", name="ck_document_findings_severity"
        ),
        CheckConstraint("length(rule_id) BETWEEN 1 AND 64", name="ck_document_findings_rule"),
        CheckConstraint("length(location) BETWEEN 1 AND 200", name="ck_document_findings_location"),
        CheckConstraint("length(preview) <= 40", name="ck_document_findings_preview"),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_document_findings_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_document_findings_version",
        ),
        Index("ix_document_findings_doc", "org_id", "document_id", "version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_document_findings_org", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    rule_id: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(Text)
    preview: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))


class DocumentScanPolicy(Base):
    __tablename__ = "document_scan_policies"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('off', 'flag', 'quarantine')", name="ck_document_scan_policies_mode"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_document_scan_policies_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "uq_document_scan_policies_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "uq_document_scan_policies_org",
            "org_id",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_document_scan_policies_org", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(Text, server_default=text("'flag'"))
    updated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_document_scan_policies_updated_by")
    )
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))


class DocumentScanRelease(Base):
    __tablename__ = "document_scan_releases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_document_scan_releases_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_document_scan_releases_version",
        ),
    )

    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_document_scan_releases_org", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    released_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_document_scan_releases_by")
    )
    released_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
