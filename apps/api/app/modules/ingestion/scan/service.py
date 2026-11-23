"""Scan stage + admin operations of M14.02 (policy, findings, quarantine, release).

Stage semantics (called by ``DefaultPipelineRunner`` between ``extract`` and ``chunk``):

* mode ``off``: nothing runs; ``flag``: findings are stored, the document is indexed normally;
  ``quarantine``: a ``high`` finding parks the revision as ``quarantined`` and the pipeline stops
  (no chunks, no embeddings, no index writes, no publication).
* Idempotent: the findings of a version are replaced in one transaction, so a resumed or re-queued
  job reaches the same state. A version an admin released skips the quarantine decision.
* Fails closed: in ``quarantine`` mode a scanner error never lets the document continue (the job
  retries, then dies); in ``flag`` mode it is logged and indexing proceeds.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.ingestion.checkpoints import fence
from app.modules.ingestion.contracts import ExtractedDocument
from app.modules.ingestion.runner_types import (
    ClaimedJob,
    JobContext,
    LeaseLost,
    PermanentJobError,
    RetryableJobError,
)
from app.modules.ingestion.scan.detectors import Finding, scan_document

logger: Final = logging.getLogger(__name__)

MODES: Final = ("off", "flag", "quarantine")
DEFAULT_MODE: Final = "flag"
STATE_QUARANTINED: Final = "quarantined"


def get_mode(session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
    """Workspace policy, else the org default, else ``flag``."""
    rows = session.execute(
        text(
            "SELECT workspace_id, mode FROM document_scan_policies"
            " WHERE org_id = :o AND (workspace_id = :w OR workspace_id IS NULL)"
        ),
        {"o": org_id, "w": workspace_id},
    ).all()
    by_ws = {r.workspace_id: r.mode for r in rows}
    mode: str = by_ws.get(workspace_id) or by_ws.get(None) or DEFAULT_MODE
    return mode


def store_findings(session: Session, claim: ClaimedJob, findings: list[Finding]) -> None:
    session.execute(
        text("DELETE FROM document_findings WHERE version_id = :v"), {"v": claim.version_id}
    )
    for f in findings:
        session.execute(
            text(
                "INSERT INTO document_findings (id, org_id, workspace_id, document_id, version_id,"
                " rule_id, severity, location, preview)"
                " VALUES (:id, :o, :w, :d, :v, :r, :s, :l, :p)"
                " ON CONFLICT (version_id, rule_id, location) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "o": claim.org_id,
                "w": claim.workspace_id,
                "d": claim.document_id,
                "v": claim.version_id,
                "r": f.rule_id,
                "s": f.severity,
                "l": f.location,
                "p": f.preview[:40],
            },
        )


def run_scan_stage(
    ctx: JobContext,
    claim: ClaimedJob,
    *,
    load_raw: Callable[[], bytes],
    load_doc: Callable[[], ExtractedDocument],
    content_type: str,
) -> bool:
    """Run the scan; ``True`` means the revision was quarantined and the pipeline must stop."""
    with ctx.session_factory(claim.org_id) as session:
        fence(session, claim)
        mode = get_mode(session, claim.org_id, claim.workspace_id)
        released = session.execute(
            text("SELECT 1 FROM document_scan_releases WHERE version_id = :v"),
            {"v": claim.version_id},
        ).first()
    if mode == "off":
        return False
    try:
        findings = scan_document(load_raw(), content_type, load_doc())
    except (LeaseLost, RetryableJobError, PermanentJobError):
        raise
    except Exception as exc:
        if mode == "quarantine":
            raise RetryableJobError("scan_failed") from exc
        logger.warning("document scan failed; indexing continues (mode=flag)")
        return False
    quarantine = (
        mode == "quarantine" and released is None and any(f.severity == "high" for f in findings)
    )
    with ctx.session_factory(claim.org_id) as session:
        fence(session, claim)
        store_findings(session, claim, findings)
        if quarantine:
            session.execute(
                text(
                    "UPDATE knowledge_revisions SET state = 'quarantined'"
                    " WHERE id = :r AND state IN ('building', 'ready')"
                ),
                {"r": claim.revision_id},
            )
    return quarantine


# ------------------------------------------------------------------------- admin operations


@dataclass(frozen=True, slots=True)
class QuarantineRow:
    document_id: uuid.UUID
    version_id: uuid.UUID
    title: str
    findings: int
    max_severity: str
    quarantined_at: object


def list_quarantine(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[QuarantineRow]:
    rows = session.execute(
        text(
            "SELECT r.document_id, r.version_id, d.title,"
            " (SELECT count(*) FROM document_findings f WHERE f.version_id = r.version_id) AS n,"
            " (SELECT CASE max(CASE f.severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END)"
            "   WHEN 3 THEN 'high' WHEN 2 THEN 'medium' ELSE 'low' END"
            "  FROM document_findings f WHERE f.version_id = r.version_id) AS sev,"
            " r.created_at"
            " FROM knowledge_revisions r JOIN documents d ON d.id = r.document_id"
            " WHERE r.org_id = :o AND r.workspace_id = :w AND r.state = 'quarantined'"
            " AND d.deleted_at IS NULL ORDER BY r.created_at DESC"
        ),
        {"o": org_id, "w": workspace_id},
    ).all()
    return [
        QuarantineRow(r.document_id, r.version_id, r.title, r.n, r.sev or "high", r.created_at)
        for r in rows
    ]


def quarantined_version(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> uuid.UUID | None:
    v: uuid.UUID | None = session.execute(
        text(
            "SELECT r.version_id FROM knowledge_revisions r"
            " JOIN documents d ON d.id = r.document_id"
            " WHERE r.org_id = :o AND r.workspace_id = :w AND r.document_id = :d"
            " AND r.state = 'quarantined' AND d.deleted_at IS NULL LIMIT 1"
        ),
        {"o": org_id, "w": workspace_id, "d": document_id},
    ).scalar_one_or_none()
    return v


def release(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Record the release and re-queue the job (it resumes at the scan stage, then indexes)."""
    session.execute(
        text(
            "INSERT INTO document_scan_releases (version_id, org_id, workspace_id, document_id,"
            " released_by) VALUES (:v, :o, :w, :d, :u) ON CONFLICT (version_id) DO NOTHING"
        ),
        {"v": version_id, "o": org_id, "w": workspace_id, "d": document_id, "u": user_id},
    )
    row = session.execute(
        text("SELECT job_id FROM ingest_release_quarantine(:v)"), {"v": version_id}
    ).first()
    return row is not None


def list_findings(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            "SELECT version_id, rule_id, severity, location, preview, created_at"
            " FROM document_findings"
            " WHERE org_id = :o AND workspace_id = :w AND document_id = :d"
            " ORDER BY version_id,"
            " CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
            " rule_id, location"
        ),
        {"o": org_id, "w": workspace_id, "d": document_id},
    ).all()
    return [dict(r._mapping) for r in rows]
