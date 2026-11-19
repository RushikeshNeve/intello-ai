"""ORM mirror of migration ``0009_connectors`` (contract m06 section 2).

RLS, grants, the partial unique indexes and the SECURITY DEFINER functions live in the migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

PROVIDER_GITHUB: Final = "github"
PROVIDERS: Final = (PROVIDER_GITHUB,)

SOURCE_STATE_ACTIVE: Final = "active"
SOURCE_STATE_REVOKED: Final = "revoked"
SOURCE_STATE_ERROR: Final = "error"
SOURCE_STATES: Final = (SOURCE_STATE_ACTIVE, SOURCE_STATE_REVOKED, SOURCE_STATE_ERROR)

SYNC_QUEUED: Final = "queued"
SYNC_RUNNING: Final = "running"
SYNC_SUCCEEDED: Final = "succeeded"
SYNC_FAILED: Final = "failed"
SYNC_CANCELLED: Final = "cancelled"
SYNC_STATES: Final = (SYNC_QUEUED, SYNC_RUNNING, SYNC_SUCCEEDED, SYNC_FAILED, SYNC_CANCELLED)
SYNC_TRIGGERS: Final = ("schedule", "manual", "webhook")

ITEM_ACTIVE: Final = "active"
ITEM_DELETED: Final = "deleted"

EVENT_SOURCE_SYNC_REQUESTED: Final = "source.sync.requested"
AGGREGATE_SOURCE: Final = "source"


class ConnectorAccount(Base):
    __tablename__ = "connector_accounts"
    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_connector_accounts_id_org_id"),
        CheckConstraint(_in("provider", PROVIDERS), name="ck_connector_accounts_provider"),
        CheckConstraint("length(label) BETWEEN 1 AND 255", name="ck_connector_accounts_label"),
        CheckConstraint(
            "(token_ciphertext IS NULL) = (key_id IS NULL)", name="ck_connector_accounts_key_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_connector_accounts_org_id", ondelete="CASCADE"),
    )
    provider: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_id: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_connector_accounts_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(_tstz())


class SourceSync(Base):
    __tablename__ = "source_syncs"
    __table_args__ = (
        CheckConstraint(_in("state", SYNC_STATES), name="ck_source_syncs_state"),
        CheckConstraint(_in("trigger", SYNC_TRIGGERS), name="ck_source_syncs_trigger"),
        ForeignKeyConstraint(
            ["source_id", "org_id"],
            ["sources.id", "sources.org_id"],
            name="fk_source_syncs_source",
            ondelete="CASCADE",
        ),
        Index(
            "uq_source_syncs_one_running",
            "source_id",
            unique=True,
            postgresql_where=text("state = 'running'"),
        ),
        Index(
            "uq_source_syncs_one_queued",
            "source_id",
            unique=True,
            postgresql_where=text("state = 'queued'"),
        ),
        Index("ix_source_syncs_source_id_created_at", "source_id", "created_at"),
        Index(
            "uq_source_syncs_delivery",
            "source_id",
            "delivery_id",
            unique=True,
            postgresql_where=text("delivery_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_source_syncs_org_id", ondelete="CASCADE"),
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    trigger: Mapped[str] = mapped_column(Text)
    delivery_id: Mapped[str | None] = mapped_column(Text)
    cursor_before: Mapped[str | None] = mapped_column(Text)
    cursor_after: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(_tstz())
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())


class SourceItem(Base):
    __tablename__ = "source_items"
    __table_args__ = (
        UniqueConstraint("source_id", "remote_id", name="uq_source_items_source_id_remote_id"),
        CheckConstraint(_in("state", (ITEM_ACTIVE, ITEM_DELETED)), name="ck_source_items_state"),
        ForeignKeyConstraint(
            ["source_id", "org_id"],
            ["sources.id", "sources.org_id"],
            name="fk_source_items_source",
            ondelete="CASCADE",
        ),
        Index("ix_source_items_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_source_items_org_id", ondelete="CASCADE"),
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    remote_id: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", name="fk_source_items_document_id")
    )
    version_token: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    last_seen_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
