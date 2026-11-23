"""Per-org firewall policy (table ``firewall_policies``, migration 0023). No row = everything off.

Modes: ``off`` (nothing runs), ``flag`` (records counts and rule ids only), ``block``; the output
guard also knows ``redact``. ``threshold`` (0.05-1.0, default 0.5) is the sensitivity of the input
guard: a text is a hit when its score is >= threshold (lower = stricter). All functions take an
RLS-scoped session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

OFF: Final = "off"
FLAG: Final = "flag"
BLOCK: Final = "block"
REDACT: Final = "redact"
INPUT_MODES: Final = (OFF, FLAG, BLOCK)
OUTPUT_MODES: Final = (OFF, FLAG, BLOCK, REDACT)
DEFAULT_THRESHOLD: Final = 0.5


class InvalidFirewallPolicy(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FirewallPolicy:
    input_user: str = OFF
    input_evidence: str = OFF
    output: str = OFF
    threshold: float = DEFAULT_THRESHOLD
    updated_at: datetime | None = None
    updated_by: uuid.UUID | None = None

    @property
    def is_default(self) -> bool:
        return self.updated_at is None


def validate(input_user: str, input_evidence: str, output: str, threshold: float) -> None:
    if input_user not in INPUT_MODES or input_evidence not in INPUT_MODES:
        raise InvalidFirewallPolicy("input mode")
    if output not in OUTPUT_MODES:
        raise InvalidFirewallPolicy("output mode")
    if not 0.05 <= threshold <= 1.0:
        raise InvalidFirewallPolicy("threshold")


def load(session: Session, org_id: uuid.UUID) -> FirewallPolicy:
    row = session.execute(
        text(
            "SELECT input_user_mode, input_evidence_mode, output_mode, threshold, updated_at,"
            " updated_by FROM firewall_policies WHERE org_id = :o"
        ),
        {"o": org_id},
    ).one_or_none()
    if row is None:
        return FirewallPolicy()
    return FirewallPolicy(
        row.input_user_mode,
        row.input_evidence_mode,
        row.output_mode,
        float(row.threshold),
        row.updated_at,
        row.updated_by,
    )


def save(
    session: Session,
    org_id: uuid.UUID,
    *,
    input_user: str,
    input_evidence: str,
    output: str,
    threshold: float,
    updated_by: uuid.UUID,
) -> FirewallPolicy:
    validate(input_user, input_evidence, output, threshold)
    session.execute(
        text(
            "INSERT INTO firewall_policies (org_id, input_user_mode, input_evidence_mode,"
            " output_mode, threshold, updated_by, updated_at)"
            " VALUES (:o, :iu, :ie, :out, :t, :by, now())"
            " ON CONFLICT (org_id) DO UPDATE SET input_user_mode = EXCLUDED.input_user_mode,"
            " input_evidence_mode = EXCLUDED.input_evidence_mode,"
            " output_mode = EXCLUDED.output_mode, threshold = EXCLUDED.threshold,"
            " updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {
            "o": org_id,
            "iu": input_user,
            "ie": input_evidence,
            "out": output,
            "t": threshold,
            "by": updated_by,
        },
    )
    return load(session, org_id)
