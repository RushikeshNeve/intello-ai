"""The outbox dispatcher (M03.01.02; contract §5): ``python -m app.workers.dispatcher``.

Each pass runs ONE transaction: ``outbox_fetch_batch(n)`` (rows locked until the transaction ends,
so two dispatchers never hold the same row) -> publish a Celery task per row, in ``seq`` order ->
``outbox_mark_dispatched`` -> commit. Delivery is therefore **at least once**: a crash after a
publish but before the commit re-sends the row, which is harmless because the receiving task claims
its job in PostgreSQL (a duplicate claims nothing). A broker outage rolls the pass back and the rows
go out when it returns. A row that cannot be published for its own reason (malformed payload, a type
this build does not route) is *quarantined*: ``outbox_mark_failed`` pushes it far into the future
with the reason in ``last_error`` instead of looping on it.

The same loop runs the lease reaper every ``INGEST_REAP_INTERVAL_SECONDS`` and the reconcile
sweeper every ``INGEST_RECONCILE_INTERVAL_SECONDS``. SIGTERM/SIGINT finish the current pass and
exit.
Liveness: the loop touches ``DISPATCHER_HEALTH_FILE`` on every iteration (also while the broker or
the database is down, because the loop itself is alive); ``--check`` exits 0 when it is fresh.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, text

from app.modules.ai import chat_repo
from app.modules.ingestion.models import (
    EVENT_CHAT_RUN_QUEUED,
    EVENT_DOCUMENT_DELETED,
    EVENT_JOB_QUEUED,
    EVENT_RECONCILE,
    EVENT_REVISION_PUBLISHED,
)
from app.workers.celery_app import (
    QUEUE_CHAT,
    QUEUE_INGEST,
    QUEUE_MAINTENANCE,
    TASK_CHAT_RUN,
    TASK_DELETE_DOCUMENT,
    TASK_RECONCILE,
    TASK_RUN_JOB,
)
from app.workers.leases import reap_expired_leases
from app.workers.settings import WorkerSettings, get_worker_settings
from app.workers.worker_db import get_worker_engine, system_connection

log = logging.getLogger("ukos.dispatcher")

TRANSIENT_BACKOFF_CAP: Final = 300
# A chat run is started at most this many times (a dead worker's run is requeued once).
CHAT_REAP_MAX_ATTEMPTS: Final = 2


class BrokerUnavailable(Exception):
    """The broker cannot be reached: leave the rows, back off, do not burn their attempts."""


class PoisonEvent(Exception):
    """This row can never be published as is (unknown type, malformed payload)."""


@dataclass(frozen=True)
class OutboxRow:
    id: uuid.UUID
    seq: int
    org_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class Delivery:
    task: str
    queue: str
    args: tuple[str, ...]


Publisher = Callable[[Delivery], None]


def _uuid(value: object, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PoisonEvent(f"malformed {what}") from exc


def _job_queued(row: OutboxRow) -> Delivery:
    return Delivery(TASK_RUN_JOB, QUEUE_INGEST, (_uuid(row.payload.get("job_id"), "job_id"),))


def _chat_run_queued(row: OutboxRow) -> Delivery:
    return Delivery(TASK_CHAT_RUN, QUEUE_CHAT, (_uuid(row.payload.get("run_id"), "run_id"),))


def _document_task(task: str) -> Callable[[OutboxRow], Delivery]:
    def build(row: OutboxRow) -> Delivery:
        document_id = row.payload.get("document_id", row.aggregate_id)
        return Delivery(
            task, QUEUE_MAINTENANCE, (str(row.org_id), _uuid(document_id, "document_id"))
        )

    return build


# ``knowledge.revision.published`` is routed to reconcile: it is idempotent and finishes exactly
# what the event is for (flag flip of the new revision, purge of the superseded one).
ROUTES: Final[Mapping[str, Callable[[OutboxRow], Delivery]]] = {
    EVENT_JOB_QUEUED: _job_queued,
    EVENT_CHAT_RUN_QUEUED: _chat_run_queued,
    EVENT_DOCUMENT_DELETED: _document_task(TASK_DELETE_DOCUMENT),
    EVENT_RECONCILE: _document_task(TASK_RECONCILE),
    EVENT_REVISION_PUBLISHED: _document_task(TASK_RECONCILE),
}


def celery_publisher(app: Any = None) -> Publisher:
    """Publisher over the Celery app (``send_task`` by name; no task module is imported)."""
    from kombu.exceptions import KombuError

    from app.workers.celery_app import celery_app as default_app

    client = app if app is not None else default_app

    def publish(delivery: Delivery) -> None:
        try:
            client.send_task(delivery.task, args=list(delivery.args), queue=delivery.queue)
        except (KombuError, OSError, ConnectionError) as exc:
            raise BrokerUnavailable(type(exc).__name__) from exc

    return publish


def transient_retry_seconds(attempts: int, cap: int = TRANSIENT_BACKOFF_CAP) -> int:
    """Deterministic exponential backoff for a row that failed for a reason of its own."""
    return int(min(cap, 2 ** min(max(attempts, 0) + 1, 20)))


class Dispatcher:
    def __init__(
        self,
        engine: Engine,
        settings: WorkerSettings,
        publish: Publisher,
        *,
        routes: Mapping[str, Callable[[OutboxRow], Delivery]] = ROUTES,
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.publish = publish
        self.routes = routes
        self.stop_event = stop or threading.Event()
        self._clock = clock
        self._next_reap = 0.0
        self._next_reconcile = 0.0
        self._last_batch_size = 0

    # -- one pass -----------------------------------------------------------------------------

    def dispatch_once(self) -> int:
        """Publish one batch. Returns the number of rows marked dispatched.

        Raises :class:`BrokerUnavailable` (after rolling the whole pass back) when the broker is
        down: the rows stay undispatched and their attempts untouched.
        """
        with system_connection(self.engine) as conn:
            fetched = (
                conn.execute(
                    text("SELECT * FROM outbox_fetch_batch(:n)"),
                    {"n": self.settings.OUTBOX_BATCH_SIZE},
                )
                .mappings()
                .all()
            )
            done: list[uuid.UUID] = []
            for raw in fetched:
                row = OutboxRow(**dict(raw))
                try:
                    route = self.routes.get(row.event_type)
                    if route is None:
                        raise PoisonEvent("unknown_event_type")
                    self.publish(route(row))
                except PoisonEvent as exc:
                    conn.execute(
                        text("SELECT outbox_mark_failed(:i, :e, :r)"),
                        {
                            "i": row.id,
                            "e": f"poison:{exc}",
                            "r": self.settings.OUTBOX_POISON_RETRY_SECONDS,
                        },
                    )
                    log.error("outbox %s seq=%s quarantined: %s", row.id, row.seq, exc)
                    continue
                except BrokerUnavailable:
                    raise  # the context manager rolls the pass back; nothing is marked
                except Exception as exc:  # noqa: BLE001 - one bad row must not stall the batch
                    conn.execute(
                        text("SELECT outbox_mark_failed(:i, :e, :r)"),
                        {
                            "i": row.id,
                            "e": f"publish_failed:{type(exc).__name__}",
                            "r": min(
                                self.settings.OUTBOX_MAX_BACKOFF_SECONDS,
                                transient_retry_seconds(row.attempts),
                            ),
                        },
                    )
                    log.warning("outbox %s publish failed: %s", row.id, type(exc).__name__)
                    continue
                done.append(row.id)
            if done:
                conn.execute(text("SELECT outbox_mark_dispatched(:ids)"), {"ids": done})
            sent = len(done)
            batch_size = len(fetched)
        self._last_batch_size = batch_size
        return sent

    # -- sweeps ---------------------------------------------------------------------------------

    def reap(self) -> int:
        return reap_expired_leases(self.engine, self.settings.INGEST_RETRY_BASE_SECONDS)

    def reap_chat(self) -> int:
        """Chat runs whose worker died: requeued once when no token was streamed yet (a new outbox
        row is written by the function), else ``failed lease_expired``."""
        with system_connection(self.engine) as conn:
            return len(chat_repo.reap_expired_runs(conn, CHAT_REAP_MAX_ATTEMPTS))

    def reconcile_sweep(self) -> int:
        """Hand documents whose publication is due for a check to the maintenance queue; the
        function marks them (``reconciled_at``) in the same transaction, so a failed publish rolls
        the marking back."""
        with system_connection(self.engine) as conn:
            due = (
                conn.execute(
                    text("SELECT * FROM knowledge_next_reconcile_batch(:n, :older)"),
                    {
                        "n": self.settings.RECONCILE_BATCH_SIZE,
                        "older": self.settings.INGEST_RECONCILE_INTERVAL_SECONDS,
                    },
                )
                .mappings()
                .all()
            )
            for item in due:
                self.publish(
                    Delivery(
                        TASK_RECONCILE,
                        QUEUE_MAINTENANCE,
                        (str(item["org_id"]), str(item["document_id"])),
                    )
                )
        return len(due)

    # -- loop -------------------------------------------------------------------------------------

    def touch_health(self) -> None:
        path = Path(self.settings.DISPATCHER_HEALTH_FILE)
        try:
            path.touch()
            os.utime(path, None)
        except OSError:
            log.warning("cannot update the health file")

    def run_forever(self) -> None:
        failures = 0
        while not self.stop_event.is_set():
            self.touch_health()
            pause = self.settings.OUTBOX_POLL_SECONDS
            try:
                sent = self.dispatch_once()
                failures = 0
                if self._last_batch_size >= self.settings.OUTBOX_BATCH_SIZE and sent > 0:
                    pause = 0  # a full batch: drain without waiting
                self._maybe_sweeps()
            except BrokerUnavailable:
                failures += 1
                pause = min(self.settings.OUTBOX_MAX_BACKOFF_SECONDS, 2 ** min(failures, 8))
                log.warning("broker unavailable; retrying in %ss", pause)
            except Exception as exc:  # noqa: BLE001 - the loop must survive a database blip
                failures += 1
                pause = min(self.settings.OUTBOX_MAX_BACKOFF_SECONDS, 2 ** min(failures, 8))
                log.error("dispatch pass failed (%s); retrying in %ss", type(exc).__name__, pause)
            if pause:
                self.stop_event.wait(pause)
        log.info("dispatcher stopped")

    def _maybe_sweeps(self) -> None:
        now = self._clock()
        if now >= self._next_reap:
            self._next_reap = now + self.settings.INGEST_REAP_INTERVAL_SECONDS
            reaped = self.reap()
            if reaped:
                log.info("reaped %s expired lease(s)", reaped)
            try:
                chat_reaped = self.reap_chat()
            except Exception as exc:  # noqa: BLE001 - never let the chat sweep stop the others
                log.error("chat reap failed (%s)", type(exc).__name__)
            else:
                if chat_reaped:
                    log.info("reaped %s expired chat run(s)", chat_reaped)
        if now >= self._next_reconcile:
            self._next_reconcile = now + self.settings.INGEST_RECONCILE_INTERVAL_SECONDS
            queued = self.reconcile_sweep()
            if queued:
                log.info("queued %s reconcile task(s)", queued)


def health_ok(settings: WorkerSettings, *, now: float | None = None) -> bool:
    try:
        mtime = Path(settings.DISPATCHER_HEALTH_FILE).stat().st_mtime
    except OSError:
        return False
    return ((now if now is not None else time.time()) - mtime) <= (
        settings.DISPATCHER_HEALTH_MAX_AGE_SECONDS
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers.dispatcher")
    parser.add_argument("--check", action="store_true", help="liveness probe (for the healthcheck)")
    args = parser.parse_args(argv)
    settings = get_worker_settings()
    if args.check:
        return 0 if health_ok(settings) else 1

    logging.basicConfig(
        level=settings.UKOS_LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    dispatcher = Dispatcher(get_worker_engine(settings), settings, celery_publisher())

    def _stop(signum: int, _frame: object) -> None:
        log.info("signal %s received; finishing the current pass", signum)
        dispatcher.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info(
        "dispatcher started (batch=%s poll=%ss)",
        settings.OUTBOX_BATCH_SIZE,
        settings.OUTBOX_POLL_SECONDS,
    )
    dispatcher.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
