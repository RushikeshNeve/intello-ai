"""ORM mirror of ``security_events``, ``canary_documents``, ``abuse_settings`` (migration 0025)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

EVENT_KINDS: Final = ("abuse_anomaly", "canary_hit")
SEVERITIES: Final = ("low", "medium", "high")
ACTIONS: Final = ("off", "flag", "throttle", "block")


class SecurityEvent(Base):
    """Ids and counts only: never a query, answer or document text."""

    __tablename__ = "security_events"
    __table_args__ = (
        CheckConstraint(_in("kind", EVENT_KINDS), name="ck_security_events_kind"),
        CheckConstraint(_in("severity", SEVERITIES), name="ck_security_events_severity"),
        Index("ix_security_events_org_created", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_security_events_org_id", ondelete="CASCADE"),
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    acknowledged_at: Mapped[datetime | None] = mapped_column(_tstz())
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class CanaryDocument(Base):
    __tablename__ = "canary_documents"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_canary_documents_org_id", ondelete="CASCADE"),
        primary_key=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", name="fk_canary_documents_document_id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())


class AbuseSettings(Base):
    __tablename__ = "abuse_settings"
    __table_args__ = (
        CheckConstraint(_in("action", ACTIONS), name="ck_abuse_settings_action"),
        CheckConstraint(
            "window_seconds BETWEEN 10 AND 86400 AND block_seconds BETWEEN 10 AND 86400"
            " AND max_search > 0 AND max_chat > 0 AND max_evidence_reads > 0 AND max_export > 0"
            " AND max_distinct_docs > 0",
            name="ck_abuse_settings_bounds",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_abuse_settings_org_id", ondelete="CASCADE"),
        primary_key=True,
    )
    action: Mapped[str] = mapped_column(Text, server_default="flag")
    window_seconds: Mapped[int] = mapped_column(Integer, server_default="300")
    block_seconds: Mapped[int] = mapped_column(Integer, server_default="900")
    max_search: Mapped[int] = mapped_column(Integer, server_default="120")
    max_chat: Mapped[int] = mapped_column(Integer, server_default="60")
    max_evidence_reads: Mapped[int] = mapped_column(Integer, server_default="200")
    max_export: Mapped[int] = mapped_column(Integer, server_default="20")
    max_distinct_docs: Mapped[int] = mapped_column(Integer, server_default="150")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
