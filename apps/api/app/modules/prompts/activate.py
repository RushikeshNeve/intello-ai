"""Prompt activation gate (M12.06).

Activating an org version needs evidence of a passing evaluation when the org enforces the gate
(``prompt_gate_settings.enforced``, default off): a workspace golden-set ``eval_run_id`` (M12.03)
whose ``pass_rate`` meets the org threshold, or a CI-signed offline attestation. Rollback and the
global default never call :func:`check_gate`, so reverting is always possible. Failures carry no
detail.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

GATED_KINDS: Final = frozenset({"chat_system", "graph_extract", "research_plan"})
DEFAULT_MIN_PASS_RATE: Final = 0.8
ATTESTATION_ENV: Final = "PROMPT_ATTESTATION_SECRET"


class EvalRequired(Exception):
    """Activation refused: no (passing) evaluation evidence. Maps to 409 ``eval_required``."""


@dataclass(frozen=True, slots=True)
class GateSettings:
    enforced: bool = False
    min_pass_rate: float = DEFAULT_MIN_PASS_RATE


def sign_attestation(kind: str, body_sha256: str, secret: str) -> str:
    """CI side: ``<sha256>.<hmac>`` over ``kind|sha256``."""
    mac = hmac.new(secret.encode(), f"{kind}|{body_sha256}".encode(), hashlib.sha256).hexdigest()
    return f"{body_sha256}.{mac}"


def attestation_valid(kind: str, body_sha256: str, attestation_id: str) -> bool:
    secret = os.environ.get(ATTESTATION_ENV, "")
    if not secret:
        return False
    return hmac.compare_digest(sign_attestation(kind, body_sha256, secret), attestation_id)


def load_settings(session: Session, org_id: uuid.UUID) -> GateSettings:
    row = session.execute(
        text("SELECT enforced, min_pass_rate FROM prompt_gate_settings WHERE org_id = :o"),
        {"o": org_id},
    ).one_or_none()
    return GateSettings() if row is None else GateSettings(bool(row[0]), float(row[1]))


def save_settings(
    session: Session, org_id: uuid.UUID, enforced: bool, min_pass_rate: float
) -> GateSettings:
    session.execute(
        text(
            "INSERT INTO prompt_gate_settings (org_id, enforced, min_pass_rate)"
            " VALUES (:o, :e, :m) ON CONFLICT (org_id) DO UPDATE"
            " SET enforced = :e, min_pass_rate = :m, updated_at = now()"
        ),
        {"o": org_id, "e": enforced, "m": min_pass_rate},
    )
    return GateSettings(enforced, min_pass_rate)


def run_passes(summary: dict[str, Any] | None, state: str, min_pass_rate: float) -> bool:
    rate = (summary or {}).get("pass_rate")
    return state == "completed" and isinstance(rate, int | float) and rate >= min_pass_rate


def check_gate(
    session: Session,
    org_id: uuid.UUID,
    version: dict[str, Any],
    eval_run_id: uuid.UUID | None,
    attestation_id: str | None,
) -> tuple[uuid.UUID | None, str | None]:
    """Return the validated evidence to store, or raise :class:`EvalRequired`. Gate off (or a kind
    that is not gated): nothing is required and nothing is stored."""
    settings = load_settings(session, org_id)
    if version["kind"] not in GATED_KINDS or not settings.enforced:
        return None, None
    if eval_run_id is not None:
        row = session.execute(
            text("SELECT state, summary FROM eval_runs WHERE id = :i AND org_id = :o"),
            {"i": eval_run_id, "o": org_id},
        ).one_or_none()
        if row is not None and run_passes(row[1], row[0], settings.min_pass_rate):
            return eval_run_id, None
    if attestation_id and attestation_valid(version["kind"], version["sha256"], attestation_id):
        return None, attestation_id
    raise EvalRequired
