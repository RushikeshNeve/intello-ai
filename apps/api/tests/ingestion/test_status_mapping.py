"""M03.03.04 pure logic: state mapping, safe error codes, SSE framing (no database)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.modules.ingestion import events_stream
from app.modules.ingestion.schemas import (
    ERROR_MESSAGES,
    FALLBACK_ERROR,
    IngestionSummary,
    error_message,
    safe_error_code,
)
from app.modules.ingestion.status_service import (
    JobInfo,
    is_retryable,
    public_state,
    summarize,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def info(
    state: str,
    code: str | None = None,
    revision: str | None = None,
    attempts: int = 0,
    stage: str | None = None,
) -> JobInfo:
    return JobInfo(
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        job_state=state,
        stage=stage,
        attempts=attempts,
        max_attempts=5,
        error_code=code,
        updated_at=NOW,
        revision_state=revision,
        published_at=None,
    )


@pytest.mark.parametrize(
    ("job", "state"),
    [
        (None, "none"),
        (info("queued"), "queued"),
        (info("running"), "processing"),
        (info("succeeded", revision="published"), "ready"),
        (info("succeeded", revision="superseded"), "ready"),
        (info("succeeded", revision="building"), "processing"),
        (info("dead", "timeout"), "failed"),
        (info("unsupported", "unsupported_type"), "unsupported"),
        (info("unsupported", "no_text_extracted"), "no_text"),
        (info("cancelled"), "cancelled"),
    ],
)
def test_public_state(job: JobInfo | None, state: str) -> None:
    assert public_state(job) == state


def test_retryable_states() -> None:
    assert is_retryable(info("dead"))
    assert is_retryable(info("unsupported", "unsupported_type"))
    assert not is_retryable(info("unsupported", "no_text_extracted"))
    assert not any(is_retryable(info(s)) for s in ("queued", "running", "succeeded", "cancelled"))
    assert not is_retryable(None)


def test_summary_hides_errors_of_live_jobs_and_gates_retry_on_write() -> None:
    live = summarize(info("queued", "timeout", attempts=2, stage="embed"))
    assert (live.error_code, live.message) == (None, None)
    assert live.attempts_label == "Retrying: attempt 3 of 5"
    failed = info("dead", "index_unavailable")
    assert summarize(failed, can_write=False).can_retry is False
    assert summarize(failed, can_write=True).can_retry is True
    assert summarize(None) == IngestionSummary(state="none")


def test_error_codes_are_a_closed_set() -> None:
    assert safe_error_code(None) is None
    assert safe_error_code("") is None
    assert safe_error_code("pdf_unreadable") == "pdf_unreadable"
    assert safe_error_code("KeyError: 'secret'") == FALLBACK_ERROR
    assert error_message("whatever") == ERROR_MESSAGES[FALLBACK_ERROR]
    for message in ERROR_MESSAGES.values():
        assert message.endswith(".")
        assert "/" not in message


def test_last_event_id_parsing() -> None:
    parse = events_stream.parse_last_event_id
    assert parse("42", None) == 42
    assert parse(None, "7") == 7
    assert parse("x", "8") == 8
    assert parse("-1", None) is None
    assert parse("9" * 40, None) is None
    assert parse(None, None) is None


def test_frames() -> None:
    raw = events_stream.frame("job", {"a": 1}, 5).decode()
    assert raw == 'event: job\nid: 5\ndata: {"a":1}\n\n'
    one_line = events_stream.frame(None, {"n": "line1\nline2"}).decode()
    assert one_line.count("\n") == 2  # JSON escapes the newline: a single data line
    end = events_stream.end_frame("terminal").decode()
    assert json.loads(end.split("data: ")[1]) == {"reason": "terminal"}
