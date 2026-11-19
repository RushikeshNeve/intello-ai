"""Settings (contract §10) and the statement shape of ``enqueue_*`` (no database needed)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.modules.ingestion.ids import PIPELINE_VERSION, compute_embedding_space_id, revision_id_for
from app.modules.ingestion.jobs import enqueue_document_deleted, enqueue_ingestion
from app.modules.ingestion.models import (
    JOB_EVENT_TYPES,
    JOB_STATES,
    OUTBOX_EVENT_TYPES,
    REVISION_STATES,
    STAGES,
)

PG = postgresql.dialect()  # type: ignore[no-untyped-call]


def test_contract_defaults() -> None:
    s = Settings()
    assert (s.INGEST_MAX_ATTEMPTS, s.INGEST_LEASE_SECONDS, s.INGEST_HEARTBEAT_SECONDS) == (
        5,
        120,
        30,
    )
    assert (s.INGEST_RETRY_BASE_SECONDS, s.INGEST_CONCURRENCY) == (10, 2)
    assert (s.INGEST_TASK_SOFT_LIMIT_SECONDS, s.INGEST_REAP_INTERVAL_SECONDS) == (900, 30)
    assert s.INGEST_RECONCILE_INTERVAL_SECONDS == 300
    assert (s.INGEST_MAX_PDF_PAGES, s.INGEST_MAX_EXTRACT_BYTES, s.INGEST_MAX_CHUNKS) == (
        2000,
        52_428_800,
        20_000,
    )
    assert (s.INGEST_CHUNK_TARGET_CHARS, s.INGEST_CHUNK_OVERLAP_CHARS) == (1800, 200)
    assert (s.INGEST_EMBED_BATCH_SIZE, s.OUTBOX_POLL_SECONDS) == (16, 1.0)
    assert s.POSTGRES_WORKER_USER == "ukos_worker"
    assert s.POSTGRES_WORKER_PASSWORD is None
    assert s.DATABASE_WORKER_URL is None
    assert s.sqlalchemy_worker_url is None


def test_embedding_identity_defaults_give_the_pinned_space_id() -> None:
    s = Settings()
    assert s.AI_EMBEDDING_MODEL == "google/embedding-gemma-300m"
    assert s.AI_EMBEDDING_DIMENSIONS == 768
    assert s.AI_EMBEDDING_QUERY_PREFIX == "task: search result | query: "
    assert s.AI_EMBEDDING_DOCUMENT_PREFIX == "title: none | text: "
    assert s.AI_EMBEDDING_SPEC_VERSION == 1
    assert s.embedding_space_id == "eb3ad37f8d916"
    assert s.embedding_space_id == compute_embedding_space_id(
        "google/embedding-gemma-300m",
        768,
        "task: search result | query: ",
        "title: none | text: ",
        1,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"AI_EMBEDDING_MODEL": "other/model"},
        {"AI_EMBEDDING_DIMENSIONS": 1024},
        {"AI_EMBEDDING_QUERY_PREFIX": "query: "},
        {"AI_EMBEDDING_DOCUMENT_PREFIX": "passage: "},
        {"AI_EMBEDDING_SPEC_VERSION": 2},
    ],
)
def test_any_identity_change_is_a_new_embedding_space(override: dict[str, Any]) -> None:
    assert Settings(**override).embedding_space_id != Settings().embedding_space_id


def test_prefixes_keep_their_trailing_space_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_EMBEDDING_QUERY_PREFIX", "task: search result | query: ")
    monkeypatch.setenv("AI_EMBEDDING_DOCUMENT_PREFIX", "title: none | text: ")
    assert Settings().embedding_space_id == "eb3ad37f8d916"


def test_worker_url_is_a_secret_and_normalised() -> None:
    s = Settings(DATABASE_WORKER_URL="postgres://ukos_worker:s3cret@db:5432/ukos")  # type: ignore[arg-type]
    assert "s3cret" not in repr(s)
    assert "s3cret" not in str(s.DATABASE_WORKER_URL)
    assert s.sqlalchemy_worker_url == "postgresql+psycopg://ukos_worker:s3cret@db:5432/ukos"


@pytest.mark.parametrize(
    "override",
    [
        {"INGEST_MAX_ATTEMPTS": 0},
        {"INGEST_LEASE_SECONDS": 1},
        {"INGEST_HEARTBEAT_SECONDS": 0},
        {"INGEST_HEARTBEAT_SECONDS": 90, "INGEST_LEASE_SECONDS": 120},  # lease must outlive a miss
        {"INGEST_CONCURRENCY": 0},
        {"INGEST_CHUNK_TARGET_CHARS": 2401},
        {"INGEST_CHUNK_OVERLAP_CHARS": 1000, "INGEST_CHUNK_TARGET_CHARS": 1800},
        {"INGEST_EMBED_BATCH_SIZE": 0},
        {"OUTBOX_POLL_SECONDS": 0},
        {"AI_EMBEDDING_DIMENSIONS": 0},
        {"POSTGRES_WORKER_USER": "Robert;DROP"},
    ],
)
def test_invalid_settings_are_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**override)


def test_state_sets_match_the_contract() -> None:
    assert JOB_STATES == ("queued", "running", "succeeded", "dead", "unsupported", "cancelled")
    assert STAGES == ("extract", "chunk", "embed", "index", "validate", "publish")
    assert REVISION_STATES == ("building", "ready", "published", "superseded", "failed", "purged")
    assert set(JOB_EVENT_TYPES) == {
        "queued", "started", "stage_started", "stage_completed", "retry_scheduled",
        "succeeded", "dead", "unsupported", "cancelled", "published",
    }  # fmt: skip
    assert set(OUTBOX_EVENT_TYPES) == {
        "ingestion.job.queued",
        "knowledge.revision.published",
        "document.deleted",
        "knowledge.reconcile",
    }


class RecordingResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value


class RecordingSession:
    """Plays the database: the first INSERT reports whether the job row was new."""

    def __init__(self, *, job_is_new: bool, existing: uuid.UUID | None = None) -> None:
        self.job_is_new = job_is_new
        self.existing = existing
        self.statements: list[Any] = []

    def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> RecordingResult:
        self.statements.append(stmt)
        compiled = str(stmt.compile(dialect=PG))
        if compiled.startswith("INSERT INTO ingestion_jobs"):
            return RecordingResult(
                stmt.compile(dialect=PG).params["id"] if self.job_is_new else None
            )
        if compiled.startswith("SELECT"):
            return RecordingResult(self.existing)
        return RecordingResult(None)


def _ids() -> dict[str, Any]:
    return {
        "org_id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "version_id": uuid.uuid4(),
        "embedding_space_id": "eb3ad37f8d916",
    }


def test_enqueue_issues_job_event_and_outbox_statements_in_order() -> None:
    ids = _ids()
    session = RecordingSession(job_is_new=True)
    job_id = enqueue_ingestion(session, **ids)  # type: ignore[arg-type]
    texts = [str(s.compile(dialect=PG)) for s in session.statements]
    assert [t.split("(")[0].strip() for t in texts] == [
        "INSERT INTO ingestion_jobs",
        "INSERT INTO ingestion_job_events",
        "INSERT INTO outbox_events",
    ]
    assert "ON CONFLICT (revision_id) DO NOTHING" in texts[0]
    assert "RETURNING ingestion_jobs.id" in texts[0]
    job_params = session.statements[0].compile(dialect=PG).params
    assert job_params["id"] == job_id
    assert job_params["revision_id"] == revision_id_for(
        ids["version_id"], PIPELINE_VERSION, ids["embedding_space_id"]
    )
    assert (job_params["state"], job_params["max_attempts"]) == ("queued", 5)
    outbox = session.statements[2].compile(dialect=PG).params
    assert outbox["event_type"] == "ingestion.job.queued"
    assert outbox["payload"] == {"job_id": str(job_id)}


def test_enqueue_of_an_existing_revision_returns_the_existing_id_and_writes_nothing_else() -> None:
    existing = uuid.uuid4()
    session = RecordingSession(job_is_new=False, existing=existing)
    assert enqueue_ingestion(session, **_ids()) == existing  # type: ignore[arg-type]
    texts = [str(s.compile(dialect=PG)) for s in session.statements]
    assert len(texts) == 2
    assert texts[0].startswith("INSERT INTO ingestion_jobs")
    assert texts[1].startswith("SELECT ingestion_jobs.id")


def test_enqueue_document_deleted_statement() -> None:
    ids = _ids()
    session = RecordingSession(job_is_new=True)
    event_id = enqueue_document_deleted(
        session,  # type: ignore[arg-type]
        org_id=ids["org_id"],
        workspace_id=ids["workspace_id"],
        document_id=ids["document_id"],
    )
    assert len(session.statements) == 1
    params = session.statements[0].compile(dialect=PG).params
    assert params["id"] == event_id
    assert params["event_type"] == "document.deleted"
    assert params["aggregate_id"] == ids["document_id"]
    assert params["payload"]["document_id"] == str(ids["document_id"])
