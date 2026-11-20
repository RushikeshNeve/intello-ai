"""Worker-side memory maintenance (M07.01.03): invalidation after a document delete, and the
periodic sweep (expiry, deleted evidence, superseded evidence). Both use definer functions, so they
act across owners without any table privilege beyond SELECT."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.workers.worker_db import system_connection


@dataclass(frozen=True, slots=True)
class SweepResult:
    expired: int
    invalidated: int


def invalidate_document(engine: Engine, org_id: uuid.UUID, document_id: uuid.UUID) -> int:
    with system_connection(engine) as conn:
        count = conn.execute(
            text("SELECT memory_invalidate_evidence(:o, CAST(:d AS uuid[]), 'evidence_deleted')"),
            {"o": org_id, "d": [str(document_id)]},
        ).scalar_one()
    return int(count or 0)


def sweep(engine: Engine) -> SweepResult:
    with system_connection(engine) as conn:
        row = conn.execute(text("SELECT * FROM memory_sweep()")).one()
    return SweepResult(int(row.expired), int(row.invalidated))
