"""Ingestion-side PII helpers (M14.04): per-workspace flags, counts by type only, optional masking.

Library surface for the ingestion pipeline / M14.02 findings: nothing here stores or returns a
matched value. Both flags default off (no row = off).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.firewall.pii.models import WorkspacePiiSettings
from app.modules.firewall.pii.redact import Redactor, count_by_type


@dataclass(frozen=True, slots=True)
class IngestPiiSettings:
    record_counts: bool = False
    mask_indexed: bool = False


def load_workspace_settings(session: Session, workspace_id: uuid.UUID) -> IngestPiiSettings:
    row = session.execute(
        select(WorkspacePiiSettings.record_counts, WorkspacePiiSettings.mask_indexed).where(
            WorkspacePiiSettings.workspace_id == workspace_id
        )
    ).one_or_none()
    return IngestPiiSettings() if row is None else IngestPiiSettings(row[0], row[1])


def scan_text(text: str, settings: IngestPiiSettings) -> tuple[str, dict[str, int]]:
    """Return ``(text_to_index, counts_by_type)``. Counts only when ``record_counts``; text is
    masked (``[TYPE_n]`` placeholders, mapping discarded) only when ``mask_indexed``."""
    counts = count_by_type(text) if settings.record_counts else {}
    if settings.mask_indexed:
        res = Redactor("placeholder").redact(text)
        return res.text, counts
    return text, counts
