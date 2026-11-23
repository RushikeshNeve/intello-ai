"""ORM mirror of migration 0023 (``firewall_policies``)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

_INPUT = ("off", "flag", "block")


class FirewallPolicyRow(Base):
    __tablename__ = "firewall_policies"
    __table_args__ = (
        CheckConstraint(_in("input_user_mode", _INPUT), name="ck_firewall_policies_input_user"),
        CheckConstraint(
            _in("input_evidence_mode", _INPUT), name="ck_firewall_policies_input_evidence"
        ),
        CheckConstraint(
            _in("output_mode", (*_INPUT, "redact")), name="ck_firewall_policies_output"
        ),
        CheckConstraint("threshold BETWEEN 0.05 AND 1.0", name="ck_firewall_policies_threshold"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_firewall_policies_org", ondelete="CASCADE"),
        primary_key=True,
    )
    input_user_mode: Mapped[str] = mapped_column(Text, server_default=text("'off'"))
    input_evidence_mode: Mapped[str] = mapped_column(Text, server_default=text("'off'"))
    output_mode: Mapped[str] = mapped_column(Text, server_default=text("'off'"))
    threshold: Mapped[float] = mapped_column(Float, server_default=text("0.5"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
