"""Seeding and SSE helpers for the ingestion status tests (everything seeded as the admin role)."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from catalog_support import Seeded, World
from sqlalchemy import text
from tenancy_support import User

from app.modules.ingestion.ids import PIPELINE_VERSION, revision_id_for

SECRET_DETAIL = "Traceback (most recent call last): s3://bucket/org/secret/key boom"
LEASE_OWNER = "worker-host-123:pid-9"

FORBIDDEN_FRAGMENTS = (
    LEASE_OWNER,
    "worker-host",
    "Traceback",
    "s3://",
    "lease_epoch",
    "lease_owner",
    "object_key",
    "run_after",
    "last_error_detail",
    SECRET_DETAIL,
)


@dataclass(frozen=True)
class SeededJob:
    job_id: uuid.UUID
    revision_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID


def seed_job(
    world: World,
    doc: Seeded,
    *,
    state: str = "queued",
    attempts: int = 0,
    max_attempts: int = 5,
    stage: str | None = None,
    error_code: str | None = None,
    epoch: int = 3,
    revision_state: str | None = None,
    events: tuple[tuple[str, str | None, int | None], ...] = (("queued", None, 0),),
    version_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> SeededJob:
    org = org_id or world.org_id
    ws = workspace_id or world.ws
    version = version_id or doc.versions[-1]
    space = "e" + uuid.uuid4().hex[:12]
    revision = revision_id_for(version, PIPELINE_VERSION, space)
    job_id = uuid.uuid4()
    running = state == "running"
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ingestion_jobs (id, org_id, workspace_id, document_id, version_id,"
                " revision_id, pipeline_version, embedding_space_id, state, attempts,"
                " max_attempts, lease_owner, lease_expires_at, lease_epoch, current_stage,"
                " last_error_code, last_error_detail, finished_at) VALUES (:id, :o, :w, :d, :v,"
                " :r, :pv, :sp, :st, :at, :ma, :lo, CASE WHEN :run THEN now() + interval"
                " '1 minute' END, :ep, :stage, :ec, :ed, CASE WHEN :fin THEN now() END)"
            ),
            {
                "id": job_id,
                "o": org,
                "w": ws,
                "d": doc.id,
                "v": version,
                "r": revision,
                "pv": PIPELINE_VERSION,
                "sp": space,
                "st": state,
                "at": attempts,
                "ma": max_attempts,
                "lo": LEASE_OWNER if running else None,
                "run": running,
                "ep": epoch,
                "stage": stage,
                "ec": error_code,
                "ed": SECRET_DETAIL if error_code else None,
                "fin": state in ("succeeded", "dead", "unsupported", "cancelled"),
            },
        )
        if revision_state is not None:
            conn.execute(
                text(
                    "INSERT INTO knowledge_revisions (id, org_id, workspace_id, document_id,"
                    " version_id, pipeline_version, embedding_space_id, state, published_at)"
                    " VALUES (:r, :o, :w, :d, :v, :pv, :sp, :st,"
                    " CASE WHEN :st = 'published' THEN now() END)"
                ),
                {
                    "r": revision,
                    "o": org,
                    "w": ws,
                    "d": doc.id,
                    "v": version,
                    "pv": PIPELINE_VERSION,
                    "sp": space,
                    "st": revision_state,
                },
            )
        for event_type, event_stage, attempt in events:
            _event(conn, org, ws, doc.id, version, job_id, event_type, event_stage, attempt)
    return SeededJob(job_id, revision, doc.id, version)


def _event(
    conn: Any,
    org: uuid.UUID,
    ws: uuid.UUID,
    document_id: uuid.UUID,
    version: uuid.UUID,
    job_id: uuid.UUID,
    event_type: str,
    stage: str | None,
    attempt: int | None,
) -> int:
    seq: int = conn.execute(
        text(
            "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id,"
            " version_id, event_type, stage, attempt, detail) VALUES (:j, :o, :w, :d, :v, :t,"
            " :s, :a, CAST(:detail AS jsonb)) RETURNING seq"
        ),
        {
            "j": job_id,
            "o": org,
            "w": ws,
            "d": document_id,
            "v": version,
            "t": event_type,
            "s": stage,
            "a": attempt,
            "detail": json.dumps({"lease_epoch": 3, "owner": LEASE_OWNER}),
        },
    ).scalar_one()
    return seq


def add_event(
    world: World,
    job: SeededJob,
    event_type: str,
    stage: str | None = None,
    attempt: int | None = 1,
) -> int:
    with world.engine.begin() as conn:
        return _event(
            conn,
            world.org_id,
            world.ws,
            job.document_id,
            job.version_id,
            job.job_id,
            event_type,
            stage,
            attempt,
        )


def set_job(world: World, job: SeededJob, **columns: Any) -> None:
    """Admin UPDATE of job columns (the worker's job in production)."""
    assigns = ", ".join(f"{name} = :{name}" for name in columns)
    extra = ", lease_owner = NULL, lease_expires_at = NULL" if "state" in columns else ""
    with world.engine.begin() as conn:
        conn.execute(
            text(f"UPDATE ingestion_jobs SET {assigns}{extra}, updated_at = now() WHERE id = :id"),
            {**columns, "id": job.job_id},
        )


def job_row(world: World, job_id: uuid.UUID) -> Any:
    return world.row("SELECT * FROM ingestion_jobs WHERE id = :i", i=job_id)


def outbox_count(world: World, job_id: uuid.UUID) -> int:
    return int(
        world.row(
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = :i"
            " AND event_type = 'ingestion.job.queued'",
            i=job_id,
        )[0]
    )


def after_delay(seconds: float, action: Callable[[], object]) -> threading.Timer:
    """Run ``action`` on a timer thread (the TestClient blocks until the stream ends)."""
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


@dataclass
class Frame:
    event: str | None
    id: str | None
    data: Any
    comment: str | None = None


def read_stream(
    world: World, path: str, user: User | None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], str, list[Frame]]:
    """Open an SSE endpoint, read it to the end, return (status, headers, raw, frames)."""
    all_headers = dict(headers or {})
    if user is not None:
        all_headers["Authorization"] = f"Bearer {user.token}"
    with world.api.client.stream("GET", path, headers=all_headers) as res:
        raw = "".join(res.iter_text())
        status = res.status_code
        out_headers = {k.lower(): v for k, v in res.headers.items()}
    return status, out_headers, raw, parse_frames(raw) if status == 200 else []


def parse_frames(raw: str) -> list[Frame]:
    frames: list[Frame] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        ident: str | None = None
        data: Any = None
        comment: str | None = None
        for line in block.split("\n"):
            if line.startswith(":"):
                comment = line[1:].strip()
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("id:"):
                ident = line[3:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
            elif line.startswith("retry:"):
                data = int(line[6:].strip())
                event = "retry"
        frames.append(Frame(event, ident, data, comment))
    return frames
