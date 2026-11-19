"""Server-sent events of a chat run (M04.02.04; contract m04 §8 and §14).

Protocol (``text/event-stream``), the same family as the ingestion stream::

    retry: 3000

    id: 1                      <- chat_run_events.seq
    event: started
    data: {}

    id: 2
    event: delta
    data: {"text": "..."}

    : keep-alive               <- comment, after HEARTBEAT_SECONDS of silence

    event: end                 <- synthetic, no id; the server closes right after
    data: {"reason": "terminal" | "timeout" | "unavailable"}

* The event log in PostgreSQL is the only state. A connection replays every event with
  ``seq > last_event_id`` (``Last-Event-ID`` header or ``?last_event_id=``; none = from the start of
  the run) and then follows new ones. Reconnecting therefore never regenerates anything and never
  repeats a ``seq``; the client dedupes by ``seq`` anyway.
* Each poll opens its own short tenant transaction that re-checks authorization (org membership,
  the run is the caller's) before reading, so no session is held while the stream waits and a user
  who lost access gets ``end: unavailable`` and nothing more. The first read happens before the 200
  is sent: another user's run is a real concealed 404.
* One connection lasts at most ``MAX_SECONDS`` (``end: timeout``); the client reconnects with
  ``Last-Event-ID``.
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
from starlette.concurrency import run_in_threadpool

from app.api.deps import TenantRequest, not_found
from app.modules.ai import chat_repo
from app.modules.ai.chat import (
    ERR_INTERNAL,
    TERMINAL_EVENT_TYPES,
    TERMINAL_STATES,
    safe_error_message,
)
from app.modules.ai.chat_service import require_member

logger = logging.getLogger("ukos.chat.events")

POLL_SECONDS: float = 0.4
HEARTBEAT_SECONDS: float = 15.0
MAX_SECONDS: float = 300.0
BATCH_LIMIT: Final = 500
RETRY_MS: Final = 3000


@dataclass(frozen=True)
class Scope:
    tenant: TenantRequest
    org_id: uuid.UUID
    run_id: uuid.UUID


@dataclass
class Poll:
    events: list[tuple[int, str, dict[str, Any]]]  # (seq, type, data)
    cursor: int
    terminal: bool
    more: bool  # a full batch was read: read again at once


def parse_last_event_id(header: str | None, query: str | None) -> int:
    """The resume position (0 = from the start). Malformed values read as 0."""
    for raw in (header, query):
        if raw is not None and raw.strip().isdigit() and len(raw.strip()) <= 18:
            return int(raw.strip())
    return 0


def _wire_data(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Event data as sent. A ``failed`` event written by the reaper has no message: fill it in."""
    if event_type == "failed":
        code = str(data.get("error_code", ERR_INTERNAL))
        return {"error_code": code, "message": str(data.get("message") or safe_error_message(code))}
    return data


def read(scope: Scope, after: int) -> Poll:
    """One short tenant transaction: authorize, then read events newer than ``after``.

    Raises the concealed 404 (``HTTPException``) when the run is not the caller's, or the caller is
    no member of the org any more. The state is read BEFORE the events: the worker writes the final
    events and the terminal state in one transaction, so seeing the state means seeing the events.
    """
    with scope.tenant.transaction(scope.org_id) as session:
        require_member(session, scope.tenant, scope.org_id)
        run = chat_repo.get_run(
            session, org_id=scope.org_id, user_id=scope.tenant.user_id, run_id=scope.run_id
        )
        if run is None:
            raise not_found()
        found = chat_repo.read_events_after(
            session,
            org_id=scope.org_id,
            run_id=scope.run_id,
            after_seq=after,
            limit=BATCH_LIMIT,
        )
    events = [(e.seq, e.type, _wire_data(e.type, e.data)) for e in found]
    terminal = run.state in TERMINAL_STATES or any(t in TERMINAL_EVENT_TYPES for _, t, _ in events)
    cursor = events[-1][0] if events else after
    return Poll(events, cursor, terminal, more=len(found) >= BATCH_LIMIT)


def frame(event_type: str, data: dict[str, Any], seq: int | None) -> bytes:
    lines: list[str] = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event_type}")
    lines.append("data: " + json.dumps(data, separators=(",", ":"), default=str))
    return ("\n".join(lines) + "\n\n").encode()


def end_frame(reason: str) -> bytes:
    return frame("end", {"reason": reason}, None)


async def stream(scope: Scope, first: Poll) -> AsyncIterator[bytes]:
    """The body of the response. ``first`` was read (and authorized) before the 200 was sent."""
    started = time.monotonic()
    yield f"retry: {RETRY_MS}\n\n".encode()
    poll = first
    last_write = time.monotonic()
    while True:
        for seq, event_type, data in poll.events:
            yield frame(event_type, data, seq)
        if poll.events:
            last_write = time.monotonic()
        cursor = poll.cursor
        if poll.terminal and not poll.more:
            yield end_frame("terminal")
            return
        if not poll.more:
            await asyncio.sleep(POLL_SECONDS)
        now = time.monotonic()
        if now - started >= MAX_SECONDS:
            yield end_frame("timeout")
            return
        if now - last_write >= HEARTBEAT_SECONDS:
            yield b": keep-alive\n\n"
            last_write = now
        try:
            poll = await run_in_threadpool(read, scope, cursor)
        except HTTPException:
            yield end_frame("unavailable")
            return
        except Exception as exc:  # noqa: BLE001 - only the class name is logged (no content)
            logger.warning("chat stream poll failed: %s", type(exc).__name__)
            yield end_frame("unavailable")
            return
