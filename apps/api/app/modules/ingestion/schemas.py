"""User-facing ingestion status models (M03.03.04, contract m03-contracts.md §11).

Nothing here exposes an internal: no lease owner, epoch, run_after, raw exception text, stack
trace, object key or pipeline/embedding identifiers. Job/revision state is mapped to a small
user-safe vocabulary and error codes are mapped through a closed table to a human message; any
code that is not in the table becomes ``processing_failed``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel

IngestionState = Literal[
    "none", "queued", "processing", "ready", "failed", "unsupported", "no_text", "cancelled"
]
STATES: Final = (
    "none",
    "queued",
    "processing",
    "ready",
    "failed",
    "unsupported",
    "no_text",
    "cancelled",
)
TERMINAL_STATES: Final = frozenset({"ready", "failed", "unsupported", "no_text", "cancelled"})

# Safe event vocabulary of the timeline (the ``ingestion_job_events`` CHECK set).
EVENT_LABELS: Final = {
    "queued": "Queued",
    "started": "Started",
    "stage_started": "Stage started",
    "stage_completed": "Stage completed",
    "retry_scheduled": "Retry scheduled",
    "succeeded": "Finished",
    "dead": "Failed",
    "unsupported": "Not supported",
    "cancelled": "Cancelled",
    "published": "Published",
}

FALLBACK_ERROR: Final = "processing_failed"
ERROR_MESSAGES: Final = {
    "unsupported_type": "This file type cannot be processed yet.",
    "no_text_extracted": "No readable text was found in this document.",
    "pdf_unreadable": "The PDF could not be read. It may be damaged or password protected.",
    "encoding_unreadable": "The text encoding of this file could not be read.",
    "too_large": "This document is too large to process.",
    "too_many_pages": "This document has too many pages to process.",
    "malformed_input": "The file appears to be damaged and could not be processed.",
    "ocr_unavailable": "Text recognition for images is not enabled on this server.",
    "transcription_unavailable": "Audio transcription is not enabled on this server.",
    "video_unavailable": "Video files are not supported yet.",
    "lease_expired": "Processing was interrupted and did not recover.",
    "timeout": "Processing took too long and was stopped.",
    "internal_error": "Processing failed unexpectedly.",
    "index_unavailable": "The search index was unavailable.",
    "index_incomplete": "The search index could not be updated completely.",
    "index_error": "The search index rejected this document.",
    "storage_unavailable": "The stored file could not be reached.",
    "source_missing": "The stored file could not be found.",
    "embedding_error": "The document could not be analysed for search.",
    "embedding_space_mismatch": "The search configuration changed during processing.",
    "organization_unavailable": "The organization is unavailable.",
    FALLBACK_ERROR: "Processing failed.",
}


def safe_error_code(code: str | None) -> str | None:
    """``None`` stays ``None``; a code outside the closed table becomes ``processing_failed``."""
    if not code:
        return None
    return code if code in ERROR_MESSAGES else FALLBACK_ERROR


def error_message(code: str | None) -> str | None:
    safe = safe_error_code(code)
    return None if safe is None else ERROR_MESSAGES[safe]


class IngestionSummary(BaseModel):
    """``Document.ingestion``: the state of the document's latest version."""

    state: IngestionState = "none"
    stage: str | None = None
    attempts_label: str | None = None
    error_code: str | None = None
    message: str | None = None
    updated_at: datetime | None = None
    can_retry: bool = False


class IngestionEventOut(BaseModel):
    id: int  # ingestion_job_events.seq: the SSE ``id`` and the ``Last-Event-ID`` cursor
    type: str
    label: str
    stage: str | None
    attempt: int | None
    at: datetime


class IngestionStatus(IngestionSummary):
    """``GET {ws}/documents/{id}/ingestion``."""

    document_id: uuid.UUID
    version_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    revision_id: uuid.UUID | None = None
    attempts: int = 0
    max_attempts: int | None = None
    published_at: datetime | None = None
    events: list[IngestionEventOut] = []
    next_after: int | None = None  # seq to pass as ``after`` for the next forward page


class IngestionRetryAccepted(BaseModel):
    """``202`` body of ``POST {ws}/documents/{id}/ingestion/retry``."""

    job_id: uuid.UUID
    ingestion: IngestionSummary
