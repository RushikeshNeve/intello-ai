"""Redacted prompt/response capture (M13.02, O2).

OFF by default. Per org (``ai_policies.capture_mode``): ``off`` | ``redacted`` (text passes through
:func:`redact`) | ``full_admin_only`` (stored as is; only org owners/admins can ever read captures).
Rows expire after ``capture_retention_days`` (default 14, max 90) and are removed with the evidence
they reference (``delete_for_document`` is called inside the document-deletion transaction).

Nothing in this module logs or audits capture text.
"""

from __future__ import annotations

import importlib
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

CAPTURE_OFF: Final = "off"
CAPTURE_REDACTED: Final = "redacted"
CAPTURE_FULL: Final = "full_admin_only"
CAPTURE_MODES: Final = (CAPTURE_OFF, CAPTURE_REDACTED, CAPTURE_FULL)
DEFAULT_RETENTION_DAYS: Final = 14
MAX_RETENTION_DAYS: Final = 90
MAX_MESSAGES_CHARS: Final = 32_000  # mirrors ck_ai_run_captures_messages
MAX_ANSWER_CHARS: Final = 16_000  # mirrors ck_ai_run_captures_answer

Redactor = Callable[[str], str]

# -------------------------------------------------------------------- redaction (pluggable)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DIGITS = re.compile(r"(?<![\w])\d(?:[ \-.]?\d){7,}(?![\w])")  # 8+ digits, spaces/dashes allowed
_KEYLIKE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|xox[abprs]|AKIA|AIza)[-_A-Za-z0-9]{12,}\b|"
    r"\bBearer\s+[A-Za-z0-9._\-]{16,}|\b[A-Za-z0-9_\-]{32,}\b"
)


def builtin_redact(value: str) -> str:
    """Conservative fallback: emails, long digit runs, API-key-like tokens."""
    value = _EMAIL.sub("[EMAIL]", value)
    value = _KEYLIKE.sub("[SECRET]", value)
    return _DIGITS.sub("[NUMBER]", value)


def _firewall_redactor() -> Redactor | None:
    try:
        module = importlib.import_module("app.modules.firewall.pii")
    except ImportError:
        return None
    fn = getattr(module, "redact", None)
    return fn if callable(fn) else None


def redact(value: str) -> str:
    """``app.modules.firewall.pii.redact`` when that module exists, else :func:`builtin_redact`."""
    fn = _firewall_redactor()
    if fn is None:
        return builtin_redact(value)
    try:
        res: Any = fn(value)
    except Exception:  # noqa: BLE001 - a failing detector falls back to the conservative regex
        return builtin_redact(value)
    # M14.04 returns ``RedactionResult``: store only its redacted text, never the object repr.
    inner = res if isinstance(res, str) else getattr(res, "text", None)
    if not isinstance(inner, str):
        return builtin_redact(value)
    # The conservative regex still runs on top: long digit runs (account numbers) and key-like
    # tokens the typed detectors do not cover stay masked, as before the PII module existed.
    return builtin_redact(inner)


# ------------------------------------------------------------------------------- settings


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    mode: str = CAPTURE_OFF
    retention_days: int = DEFAULT_RETENTION_DAYS


def load_settings(session: Session, org_id: uuid.UUID) -> CaptureSettings:
    row = session.execute(
        text("SELECT capture_mode, capture_retention_days FROM ai_policies WHERE org_id = :o"),
        {"o": org_id},
    ).one_or_none()
    if row is None:
        return CaptureSettings()
    return CaptureSettings(str(row[0]), int(row[1]))


def save_settings(
    session: Session,
    org_id: uuid.UUID,
    *,
    mode: str | None,
    retention_days: int | None,
    updated_by: uuid.UUID,
) -> CaptureSettings:
    """Update the capture columns of the org's policy row (created with defaults when absent).
    ``None`` keeps the current value. Raises ``ValueError`` for an invalid mode/retention."""
    current = load_settings(session, org_id)
    new_mode = current.mode if mode is None else mode
    new_days = current.retention_days if retention_days is None else retention_days
    if new_mode not in CAPTURE_MODES or not 1 <= new_days <= MAX_RETENTION_DAYS:
        raise ValueError("invalid capture settings")
    session.execute(
        text(
            "INSERT INTO ai_policies (org_id, capture_mode, capture_retention_days, updated_by)"
            " VALUES (:o, :m, :d, :u) ON CONFLICT (org_id) DO UPDATE SET"
            " capture_mode = EXCLUDED.capture_mode,"
            " capture_retention_days = EXCLUDED.capture_retention_days,"
            " updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {"o": org_id, "m": new_mode, "d": new_days, "u": updated_by},
    )
    return CaptureSettings(new_mode, new_days)


# --------------------------------------------------------------------------------- writing


def _limit(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def record_capture(
    session: Session,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    prompt_version_id: uuid.UUID | None,
    messages: Iterable[tuple[str, str]],
    answer: str,
    chunk_ids: Sequence[uuid.UUID],
    document_ids: Sequence[uuid.UUID],
    redactor: Redactor | None = None,
) -> bool:
    """Store one capture when the org's mode is not ``off``. Returns whether a row was written.
    The caller's session is an org-scoped (RLS) worker or tenant session."""
    cfg = load_settings(session, org_id)
    if cfg.mode == CAPTURE_OFF:
        return False
    clean: Redactor = (lambda s: s) if cfg.mode == CAPTURE_FULL else (redactor or redact)
    joined = "\n".join(f"[{role}] {content}" for role, content in messages)
    result = session.execute(
        text(
            "INSERT INTO ai_run_captures (org_id, workspace_id, run_id, mode, prompt_version_id,"
            " messages, answer, chunk_ids, document_ids, expires_at)"
            " VALUES (:o, :w, :r, :m, :p, :msgs, :ans, :c, :d,"
            " now() + make_interval(days => :days)) ON CONFLICT (run_id, kind) DO NOTHING"
        ),
        {
            "o": org_id,
            "w": workspace_id,
            "r": run_id,
            "m": cfg.mode,
            "p": prompt_version_id,
            "msgs": _limit(clean(_limit(joined, MAX_MESSAGES_CHARS * 2)), MAX_MESSAGES_CHARS),
            "ans": _limit(clean(_limit(answer, MAX_ANSWER_CHARS * 2)), MAX_ANSWER_CHARS),
            "c": list(chunk_ids),
            "d": sorted(set(document_ids)),
            "days": cfg.retention_days,
        },
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


# -------------------------------------------------------------------------- reading/deleting


_COLS = "id, run_id, workspace_id, kind, mode, prompt_version_id, chunk_ids, created_at, expires_at"


def list_captures(session: Session, org_id: uuid.UUID, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            f"SELECT {_COLS} FROM ai_run_captures WHERE org_id = :o"  # noqa: S608
            " AND expires_at > now() ORDER BY created_at DESC, id LIMIT :n"
        ),
        {"o": org_id, "n": limit},
    ).mappings()
    return [dict(r) for r in rows]


def get_capture(
    session: Session, org_id: uuid.UUID, capture_id: uuid.UUID
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"SELECT {_COLS}, messages, answer FROM ai_run_captures"  # noqa: S608
                " WHERE org_id = :o AND id = :i AND expires_at > now()"
            ),
            {"o": org_id, "i": capture_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


def delete_capture(session: Session, org_id: uuid.UUID, capture_id: uuid.UUID) -> bool:
    result = session.execute(
        text("DELETE FROM ai_run_captures WHERE org_id = :o AND id = :i"),
        {"o": org_id, "i": capture_id},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def delete_for_document(session: Session, document_id: uuid.UUID) -> int:
    """Remove every capture (of the session's org) that used a chunk of ``document_id``; called in
    the document-deletion transaction so a capture never outlives its evidence."""
    result = session.execute(
        text("DELETE FROM ai_run_captures WHERE :d = ANY(document_ids)"), {"d": document_id}
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def sweep_expired(engine: Engine) -> int:
    """Maintenance: delete expired captures of every org (definer function)."""
    from app.workers.worker_db import system_connection

    with system_connection(engine) as conn:
        return int(conn.execute(text("SELECT ai_run_captures_sweep()")).scalar_one())
