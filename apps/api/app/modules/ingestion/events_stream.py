"""Server-sent events for ingestion status (M03.03.04, contract m03-contracts.md §11).

Protocol (``text/event-stream``)::

    retry: 3000
    event: snapshot            <- always first; ``id:`` only on a fresh connect (the cursor)
    data: {"cursor": 42, "document_id": "...", "version_id": "...", "ingestion": {...}}

    event: job
    id: 43                     <- ingestion_job_events.seq
    data: {"seq": 43, "document_id": "...", "version_id": "...", "type": "stage_started",
           "label": "...", "stage": "embed", "attempt": 1, "at": "...", "ingestion": {...}}

    : keep-alive               <- comment, every HEARTBEAT_SECONDS of silence

    event: end                 <- the server closes right after; the client must NOT reconnect
    data: {"reason": "terminal" | "timeout" | "unavailable"}

* State comes only from PostgreSQL; nothing depends on server memory, so any reconnect (also to a
  different API instance) is consistent. A fresh connect starts at the newest event (the snapshot
  carries the current state); ``Last-Event-ID`` (or ``?last_event_id=``) replays later events,
  bounded by ``REPLAY_LIMIT``; past the bound the client gets a fresh snapshot and resumes at the
  newest event.
* Each poll runs its **own short tenant transaction** that re-checks authorization (org
  membership, workspace role, document grant, document not deleted) before reading. No database
  session or connection is held while the stream waits, so a vanished client holds nothing.
* A document stream ends (``terminal``) once the latest version's state is final; both streams end
  after ``MAX_SECONDS`` (``timeout``). Lost access ends the stream with ``unavailable`` and no
  further detail (the same word for "removed", "deleted" and "never existed").
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import TenantRequest
from app.modules.authorization.policy import Action
from app.modules.documents import catalog_service as svc
from app.modules.ingestion import status_service as ing
from app.modules.ingestion.models import IngestionJob
from app.modules.ingestion.schemas import IngestionSummary

logger = logging.getLogger("ukos.ingestion.events")

POLL_SECONDS: float = 2.0
HEARTBEAT_SECONDS: float = 15.0
MAX_SECONDS: float = 300.0
REPLAY_LIMIT: Final = 500
BATCH_LIMIT: Final = 200
SNAPSHOT_DOCUMENTS: Final = 200
RETRY_MS: Final = 3000


@dataclass(frozen=True)
class Scope:
    tenant: TenantRequest
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID | None  # ``None`` = every document of the workspace


@dataclass
class Poll:
    snapshot: dict[str, Any] | None
    events: list[dict[str, Any]]
    cursor: int
    terminal: bool


def parse_last_event_id(header: str | None, query: str | None) -> int | None:
    for raw in (header, query):
        if raw is not None and raw.strip().isdigit() and len(raw.strip()) <= 18:
            return int(raw.strip())
    return None


def _summary_json(summary: IngestionSummary) -> dict[str, Any]:
    return summary.model_dump(mode="json")


def _authorize(session: Session, scope: Scope) -> svc.WorkspaceCtx:
    """The same checks as the REST reads, re-run on every poll (raises the audited 404/403)."""
    ctx = svc.resolve_workspace(
        scope.tenant, session, scope.org_id, scope.workspace_id, Action.DOCUMENT_READ
    )
    if scope.document_id is not None:
        svc.authorize_document(session, ctx, scope.document_id, Action.DOCUMENT_READ)
    else:  # a user with only document grants has no workspace role: a concealed 404 here
        ctx.require(
            Action.DOCUMENT_READ,
            target_type=svc.TARGET_WORKSPACE,
            target_id=str(scope.workspace_id),
        )
    return ctx


def _active_document_ids(session: Session, scope: Scope) -> list[uuid.UUID]:
    rows = session.execute(
        select(IngestionJob.document_id)
        .where(
            IngestionJob.org_id == scope.org_id,
            IngestionJob.workspace_id == scope.workspace_id,
            IngestionJob.state.in_(("queued", "running")),
        )
        .distinct()
        .limit(SNAPSHOT_DOCUMENTS)
    ).all()
    return [r[0] for r in rows]


def _snapshot(session: Session, scope: Scope, cursor: int) -> tuple[dict[str, Any], bool]:
    if scope.document_id is not None:
        info = ing.latest_job(session, scope.org_id, scope.workspace_id, scope.document_id)
        summary = ing.summarize(info)
        body: dict[str, Any] = {
            "cursor": cursor,
            "document_id": str(scope.document_id),
            "version_id": None if info is None else str(info.version_id),
            "ingestion": _summary_json(summary),
        }
        return body, ing.is_terminal(summary.state)
    jobs = ing.latest_jobs(
        session, scope.org_id, scope.workspace_id, _active_document_ids(session, scope)
    )
    docs = [
        {
            "document_id": str(d),
            "version_id": str(i.version_id),
            "ingestion": _summary_json(ing.summarize(i)),
        }
        for d, i in jobs.items()
    ]
    return {"cursor": cursor, "workspace_id": str(scope.workspace_id), "documents": docs}, False


def read(scope: Scope, cursor: int | None, *, resumed: bool, with_snapshot: bool) -> Poll:
    """One short tenant transaction: authorize, read events newer than ``cursor``.

    ``cursor`` ``None`` = fresh connect: start at the newest event. A resume that is further behind
    than ``REPLAY_LIMIT`` events skips to the newest event (the snapshot carries the state).
    """
    with scope.tenant.transaction(scope.org_id) as session:
        _authorize(session, scope)
        tip = ing.max_event_seq(session, scope.org_id, scope.workspace_id, scope.document_id)
        position = tip if cursor is None else cursor
        limit = REPLAY_LIMIT + 1 if resumed else BATCH_LIMIT
        found = ing.stream_events_after(
            session,
            scope.org_id,
            scope.workspace_id,
            after=position,
            limit=limit,
            document_id=scope.document_id,
        )
        if resumed and len(found) > REPLAY_LIMIT:
            found, position = [], tip
        snapshot, terminal = (
            _snapshot(session, scope, tip if cursor is None else position)
            if with_snapshot
            else (None, False)
        )
        jobs = ing.latest_jobs(
            session,
            scope.org_id,
            scope.workspace_id,
            sorted({e.document_id for e in found}, key=str)
            if scope.document_id is None
            else [scope.document_id],
        )
        events: list[dict[str, Any]] = []
        for event in found:
            payload = event.out.model_dump(mode="json")
            payload.pop("id")
            events.append(
                {
                    "seq": event.seq,
                    "document_id": str(event.document_id),
                    "version_id": str(event.version_id),
                    **payload,
                    "ingestion": _summary_json(ing.summarize(jobs.get(event.document_id))),
                }
            )
        if found:
            position = found[-1].seq
        if scope.document_id is not None and not with_snapshot:
            terminal = ing.is_terminal(ing.summarize(jobs.get(scope.document_id)).state)
        return Poll(snapshot, events, position, terminal)


def frame(event: str | None, data: dict[str, Any], event_id: int | None = None) -> bytes:
    lines: list[str] = []
    if event is not None:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("data: " + json.dumps(data, separators=(",", ":"), default=str))
    return ("\n".join(lines) + "\n\n").encode()


def end_frame(reason: str) -> bytes:
    return frame("end", {"reason": reason})


def _job_frame(event: dict[str, Any]) -> bytes:
    return frame("job", event, event["seq"])


async def stream(scope: Scope, first: Poll, *, fresh: bool) -> AsyncIterator[bytes]:
    """The body of the response. ``first`` was read (and authorized) before the 200 was sent."""
    started = time.monotonic()
    yield f"retry: {RETRY_MS}\n\n".encode()
    yield frame("snapshot", first.snapshot or {}, first.cursor if fresh else None)
    for event in first.events:
        yield _job_frame(event)
    cursor = first.cursor
    if first.terminal:
        yield end_frame("terminal")
        return
    last_write = time.monotonic()
    while True:
        await asyncio.sleep(POLL_SECONDS)
        now = time.monotonic()
        if now - started >= MAX_SECONDS:
            yield end_frame("timeout")
            return
        try:
            poll = await run_in_threadpool(read, scope, cursor, resumed=False, with_snapshot=False)
        except HTTPException:
            yield end_frame("unavailable")
            return
        except Exception:
            logger.warning("ingestion stream poll failed", exc_info=True)
            yield end_frame("unavailable")
            return
        for event in poll.events:
            yield _job_frame(event)
        cursor = poll.cursor
        if poll.events:
            last_write = now
        if poll.terminal:
            yield end_frame("terminal")
            return
        if now - last_write >= HEARTBEAT_SECONDS:
            yield b": keep-alive\n\n"
            last_write = now
