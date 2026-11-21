"""ORM mirror of ``usage_events`` (migration ``0012_usage_events``); RLS and grants live there."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

KINDS: Final = ("chat", "embed", "research", "graph")
LIMIT_KINDS: Final = (*KINDS, "all")


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(_in("kind", KINDS), name="ck_usage_events_kind"),
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0 AND tokens_cached >= 0"
            " AND latency_ms >= 0 AND est_cost_micros >= 0",
            name="ck_usage_events_nonneg",
        ),
        Index("ix_usage_events_org_created", "org_id", "created_at"),
        Index("ix_usage_events_org_user_created", "org_id", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_usage_events_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, server_default="0")
    tokens_cached: Mapped[int] = mapped_column(Integer, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, server_default="0")
    est_cost_micros: Mapped[int] = mapped_column(BigInteger, server_default="0")
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())


class UsageLimit(Base):
    """ORM mirror of ``usage_limits`` (migration ``0015_usage_limits``, M11.08)."""

    __tablename__ = "usage_limits"
    __table_args__ = (
        CheckConstraint(_in("scope", ("org", "user")), name="ck_usage_limits_scope"),
        CheckConstraint("scope = 'user' OR user_id IS NULL", name="ck_usage_limits_org_no_user"),
        CheckConstraint(_in("kind", LIMIT_KINDS), name="ck_usage_limits_kind"),
        CheckConstraint(_in("period", ("day", "month")), name="ck_usage_limits_period"),
        CheckConstraint(_in("action", ("warn", "block")), name="ck_usage_limits_action"),
        CheckConstraint(
            "(max_tokens IS NOT NULL OR max_requests IS NOT NULL)"
            " AND coalesce(max_tokens, 0) >= 0 AND coalesce(max_requests, 0) >= 0",
            name="ck_usage_limits_max",
        ),
        Index("ix_usage_limits_org", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_usage_limits_org_id", ondelete="CASCADE"),
    )
    scope: Mapped[str] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text)
    period: Mapped[str] = mapped_column(Text)
    max_tokens: Mapped[int | None] = mapped_column(BigInteger)
    max_requests: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
