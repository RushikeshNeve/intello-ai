"""ORM mirror of ``tenant_keys`` (migration ``0026_tenant_keys``). RLS and grants live in the
migration. ``wrapped_key`` is the DEK wrapped by the master key; the plaintext DEK is never
stored."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _tstz


class TenantKey(Base):
    __tablename__ = "tenant_keys"
    __table_args__ = (
        CheckConstraint("state IN ('active', 'retired')", name="ck_tenant_keys_state"),
        CheckConstraint("octet_length(wrapped_key) BETWEEN 40 AND 4096", name="ck_tenant_keys_len"),
        Index(
            "uq_tenant_keys_one_active",
            "org_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_tenant_keys_org_id", ondelete="CASCADE"),
    )
    kek_id: Mapped[str] = mapped_column(Text)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    state: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    retired_at: Mapped[datetime | None] = mapped_column(_tstz())
