"""ORM mirror of ``embedding_cache`` (migration ``0013_embedding_cache``). RLS and grants live in
the migration."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _tstz


class EmbeddingCacheRow(Base):
    __tablename__ = "embedding_cache"
    __table_args__ = (
        CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_embedding_cache_sha"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_embedding_cache_org_id", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding_space_id: Mapped[str] = mapped_column(Text, primary_key=True)
    text_sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    last_used_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
