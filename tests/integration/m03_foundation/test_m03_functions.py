"""Behaviour of the six SECURITY DEFINER functions, called as ``ukos_worker`` (M03.01.01, §4).

The functions work across orgs, so the tests first neutralise leftovers of other tests (running
jobs, undispatched outbox rows, unreconciled publications) with the admin role.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from m03_support import (
    Scope,
    claim,
    insert_job,
    insert_outbox,
    insert_publication,
    insert_revision,
    role_tx,
    rows,
    scalar,
    seed_scope,
    sqlstate,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, OperationalError


@pytest.fixture(autouse=True)
def clean_slate(admin_engine: Engine, migrated: None) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET state = 'cancelled', lease_owner = NULL"
                " WHERE state = 'running'"
            )
        )
        conn.execute(
            text("UPDATE outbox_events SET dispatched_at = now() WHERE dispatched_at IS NULL")
        )
        conn.execute(text("UPDATE document_publications SET reconciled_at = now()"))


@pytest.fixture
def scope(admin_engine: Engine) -> Scope:
    return seed_scope(admin_engine)


def job_row(engine: Engine, job: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as conn:
        return rows(conn, "SELECT * FROM ingestion_jobs WHERE id = :j", j=job)[0]


def events(engine: Engine, job: uuid.UUID) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return rows(
            conn,
            "SELECT event_type, attempt, stage, detail, org_id, document_id, version_id"
            " FROM ingestion_job_events WHERE job_id = :j ORDER BY seq",
            j=job,
        )


def outbox_for(engine: Engine, job: uuid.UUID) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return rows(conn, "SELECT * FROM outbox_events WHERE aggregate_id = :j", j=job)


# =============================================================================================
# ingest_claim_job
# =============================================================================================


def test_claim_takes_a_due_queued_job(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    # No org context at all: the function discovers across orgs and returns ids only.
    with role_tx(worker_engine, None) as conn:
        got = claim(conn, job, owner="worker-1", lease_seconds=90)
    assert len(got) == 1
    result = got[0]
    assert set(result) == {
        "job_id", "org_id", "workspace_id", "document_id", "version_id", "revision_id",
        "attempts", "lease_epoch",
    }  # fmt: skip
    row = job_row(admin_engine, job)
    assert result == {
        "job_id": job,
        "org_id": scope.org_id,
        "workspace_id": scope.workspace_id,
        "document_id": scope.document_id,
        "version_id": scope.version_id,
        "revision_id": row["revision_id"],
        "attempts": 1,
        "lease_epoch": 1,
    }
    assert (row["state"], row["lease_owner"], row["attempts"], row["lease_epoch"]) == (
        "running",
        "worker-1",
        1,
        1,
    )
    assert row["heartbeat_at"] is not None
    assert row["started_at"] is not None
    with admin_engine.connect() as conn:
        seconds = scalar(
            conn,
            "SELECT extract(epoch FROM lease_expires_at - heartbeat_at) FROM ingestion_jobs"
            " WHERE id = :j",
            j=job,
        )
    assert float(seconds) == pytest.approx(90, abs=1)
    started = [e for e in events(admin_engine, job) if e["event_type"] == "started"]
    assert len(started) == 1
    assert (started[0]["attempt"], started[0]["org_id"]) == (1, scope.org_id)
    assert started[0]["detail"] == {"lease_epoch": 1}


def test_duplicate_claim_is_a_noop(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    with role_tx(worker_engine, None) as conn:
        assert len(claim(conn, job, "w1")) == 1
    with role_tx(worker_engine, None) as conn:
        assert claim(conn, job, "w2") == []
    row = job_row(admin_engine, job)
    assert (row["lease_owner"], row["attempts"], row["lease_epoch"]) == ("w1", 1, 1)
    assert len([e for e in events(admin_engine, job) if e["event_type"] == "started"]) == 1


def test_claim_of_a_job_not_yet_due_returns_nothing(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope, run_after_seconds=3600)
    with role_tx(worker_engine, None) as conn:
        assert claim(conn, job) == []
    row = job_row(admin_engine, job)
    assert (row["state"], row["attempts"], row["lease_epoch"]) == ("queued", 0, 0)
    assert events(admin_engine, job) == []


def test_claim_of_an_unknown_job_returns_nothing(worker_engine: Engine, migrated: None) -> None:
    with role_tx(worker_engine, None) as conn:
        assert claim(conn, uuid.uuid4()) == []


@pytest.mark.parametrize("state", ["running", "succeeded", "dead", "unsupported", "cancelled"])
def test_claim_only_takes_queued_jobs(
    worker_engine: Engine, admin_engine: Engine, scope: Scope, state: str
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(
            conn, scope, state=state, lease_owner="w0" if state == "running" else None,
            lease_expires_in=60 if state == "running" else None,
        )  # fmt: skip
    with role_tx(worker_engine, None) as conn:
        assert claim(conn, job) == []
    assert job_row(admin_engine, job)["state"] == state


def test_claim_increments_attempts_and_epoch_on_every_claim(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    for expected in (1, 2, 3):
        with role_tx(worker_engine, None) as conn:
            got = claim(conn, job)
        assert [(g["attempts"], g["lease_epoch"]) for g in got] == [(expected, expected)]
        with admin_engine.begin() as conn:  # the worker "retries": back to queued
            conn.execute(
                text(
                    "UPDATE ingestion_jobs SET state = 'queued', lease_owner = NULL WHERE id = :j"
                ),
                {"j": job},
            )
    started = [e for e in events(admin_engine, job) if e["event_type"] == "started"]
    assert [e["attempt"] for e in started] == [1, 2, 3]
    assert job_row(admin_engine, job)["started_at"] is not None


@pytest.mark.parametrize(
    ("owner", "lease"), [("", 60), ("w", 0), ("w", -5)], ids=["no-owner", "zero", "negative"]
)
def test_claim_validates_its_arguments(
    worker_engine: Engine, admin_engine: Engine, scope: Scope, owner: str, lease: int
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, None) as conn:
        claim(conn, job, owner, lease)
    assert sqlstate(exc.value) == "22023"
    assert job_row(admin_engine, job)["state"] == "queued"


def test_a_locked_job_is_skipped_not_waited_for(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    """Txn A claims (uncommitted); txn B's claim of the same job returns 0 rows immediately."""
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    with role_tx(worker_engine, None) as a:
        assert len(claim(a, job, "a")) == 1
        started = time.monotonic()
        with role_tx(worker_engine, None) as b:
            assert claim(b, job, "b") == []
        assert time.monotonic() - started < 5  # SKIP LOCKED: no waiting on A
    row = job_row(admin_engine, job)
    assert (row["lease_owner"], row["lease_epoch"]) == ("a", 1)


@pytest.mark.parametrize("claimers", [2, 8])
def test_concurrent_claimers_exactly_one_wins(
    worker_engine: Engine, admin_engine: Engine, scope: Scope, claimers: int
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    barrier = threading.Barrier(claimers)

    def attempt(n: int) -> list[dict[str, Any]]:
        with role_tx(worker_engine, None) as conn:
            barrier.wait(timeout=20)
            return claim(conn, job, f"w{n}")

    with ThreadPoolExecutor(max_workers=claimers) as pool:
        results = [f.result(timeout=60) for f in [pool.submit(attempt, n) for n in range(claimers)]]
    winners = [r for r in results if r]
    assert len(winners) == 1
    assert sum(len(r) for r in results) == 1
    row = job_row(admin_engine, job)
    assert (row["state"], row["attempts"], row["lease_epoch"]) == ("running", 1, 1)
    assert row["lease_owner"] in {f"w{n}" for n in range(claimers)}
    assert len([e for e in events(admin_engine, job) if e["event_type"] == "started"]) == 1


# =============================================================================================
# ingest_reap_expired_leases
# =============================================================================================


def reap(engine: Engine, base: int = 10) -> int:
    with role_tx(engine, None) as conn:
        return int(scalar(conn, "SELECT ingest_reap_expired_leases(:b)", b=base))


def expired(conn: Any, scope: Scope, **over: Any) -> uuid.UUID:
    return insert_job(
        conn, scope, state="running", lease_owner="dead-worker", lease_expires_in=-30, **over
    )


def test_reap_requeues_an_expired_lease_with_backoff_and_an_outbox_row(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = expired(conn, scope, attempts=1, lease_epoch=1)
    assert reap(worker_engine, base=10) == 1
    row = job_row(admin_engine, job)
    assert (row["state"], row["lease_owner"], row["lease_expires_at"]) == ("queued", None, None)
    assert (row["attempts"], row["lease_epoch"]) == (1, 1)  # only claim moves them
    assert row["last_error_code"] == "lease_expired"
    with admin_engine.connect() as conn:
        delay = float(
            scalar(
                conn,
                "SELECT extract(epoch FROM run_after - now()) FROM ingestion_jobs WHERE id = :j",
                j=job,
            )
        )
    assert 5 < delay <= 10  # base * 2^(attempts-1) = 10 s from the reap
    assert [e["event_type"] for e in events(admin_engine, job)] == ["retry_scheduled"]
    sched = events(admin_engine, job)[0]
    assert sched["attempt"] == 1
    assert sched["detail"]["reason"] == "lease_expired"
    out = outbox_for(admin_engine, job)
    assert len(out) == 1
    assert (out[0]["event_type"], out[0]["org_id"], out[0]["dispatched_at"]) == (
        "ingestion.job.queued",
        scope.org_id,
        None,
    )
    assert out[0]["payload"] == {"job_id": str(job)}
    assert out[0]["available_at"] == row["run_after"]  # dispatched when the retry is due


@pytest.mark.parametrize(
    ("attempts", "base", "expected"),
    [(1, 10, 10), (2, 10, 20), (3, 10, 40), (5, 10, 160), (30, 10, 3600), (2, 0, 0)],
)
def test_reap_backoff_is_exponential_and_capped(
    worker_engine: Engine,
    admin_engine: Engine,
    scope: Scope,
    attempts: int,
    base: int,
    expected: int,
) -> None:
    with admin_engine.begin() as conn:
        job = expired(conn, scope, attempts=attempts, max_attempts=100)
    reap(worker_engine, base=base)
    with admin_engine.connect() as conn:
        delay = float(
            scalar(
                conn,
                "SELECT extract(epoch FROM run_after - now()) FROM ingestion_jobs WHERE id = :j",
                j=job,
            )
        )
    assert delay == pytest.approx(expected, abs=3) or (expected == 0 and delay <= 0.5)


def test_reap_marks_exhausted_jobs_dead_without_an_outbox_row(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = expired(conn, scope, attempts=3, max_attempts=3)
    assert reap(worker_engine) == 1
    row = job_row(admin_engine, job)
    assert (row["state"], row["lease_owner"], row["last_error_code"]) == (
        "dead",
        None,
        "lease_expired",
    )
    assert row["finished_at"] is not None
    assert [e["event_type"] for e in events(admin_engine, job)] == ["dead"]
    assert outbox_for(admin_engine, job) == []


def test_reap_leaves_live_leases_and_other_states_alone(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        live = insert_job(conn, scope, state="running", lease_owner="w", lease_expires_in=600)
        queued = insert_job(conn, scope)
        done = insert_job(conn, scope, state="succeeded")
        stale = expired(conn, scope)
    assert reap(worker_engine) == 1  # only the stale one
    assert job_row(admin_engine, live)["state"] == "running"
    assert job_row(admin_engine, queued)["state"] == "queued"
    assert job_row(admin_engine, done)["state"] == "succeeded"
    assert job_row(admin_engine, stale)["state"] == "queued"
    assert reap(worker_engine) == 0  # nothing left to reap


def test_reap_counts_every_job_across_orgs(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    other = seed_scope(admin_engine)
    with admin_engine.begin() as conn:
        a = expired(conn, scope, attempts=1)
        b = expired(conn, other, attempts=1)
        c = expired(conn, other, attempts=5, max_attempts=5)
    assert reap(worker_engine) == 3
    assert [job_row(admin_engine, j)["state"] for j in (a, b, c)] == ["queued", "queued", "dead"]


def test_a_reaped_job_can_be_claimed_again_with_a_higher_epoch(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    with role_tx(worker_engine, None) as conn:
        assert claim(conn, job, "w1", 60)[0]["lease_epoch"] == 1
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET lease_expires_at = now() - interval '1 minute'"
                " WHERE id = :j"
            ),
            {"j": job},
        )
    assert reap(worker_engine, base=0) == 1
    time.sleep(0.05)
    with role_tx(worker_engine, None) as conn:
        got = claim(conn, job, "w2", 60)
    assert [(g["attempts"], g["lease_epoch"]) for g in got] == [(2, 2)]
    row = job_row(admin_engine, job)
    assert (row["state"], row["lease_owner"]) == ("running", "w2")
    assert [e["event_type"] for e in events(admin_engine, job)] == [
        "started",
        "retry_scheduled",
        "started",
    ]


def test_concurrent_reapers_reap_each_job_once(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        jobs = [expired(conn, scope, attempts=1) for _ in range(4)]
    with role_tx(worker_engine, None) as a:
        assert int(scalar(a, "SELECT ingest_reap_expired_leases(10)")) == 4
        with role_tx(worker_engine, None) as b:  # A's rows are locked: B skips them
            assert int(scalar(b, "SELECT ingest_reap_expired_leases(10)")) == 0
    assert all(len(outbox_for(admin_engine, j)) == 1 for j in jobs)


def test_reap_rejects_a_negative_backoff(worker_engine: Engine, migrated: None) -> None:
    with pytest.raises(DBAPIError) as exc:
        reap(worker_engine, base=-1)
    assert sqlstate(exc.value) == "22023"


# =============================================================================================
# outbox_fetch_batch / outbox_mark_dispatched / outbox_mark_failed
# =============================================================================================


def fetch(conn: Any, limit: int) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM outbox_fetch_batch(:n)", n=limit)


def test_fetch_returns_due_undispatched_rows_in_order(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    other = seed_scope(admin_engine)
    with admin_engine.begin() as conn:
        first = insert_outbox(conn, scope)
        second = insert_outbox(conn, other, event_type="document.deleted")
        insert_outbox(conn, scope, dispatched=True)  # already sent
        insert_outbox(conn, scope, available_in=3600)  # not due yet
        third = insert_outbox(conn, scope, event_type="knowledge.reconcile")
    with role_tx(worker_engine, None) as conn:
        got = fetch(conn, 10)
    assert [g["id"] for g in got] == [first, second, third]  # by seq, across orgs
    assert set(got[0]) == {
        "id", "seq", "org_id", "aggregate_type", "aggregate_id", "event_type", "payload",
        "attempts",
    }  # fmt: skip
    assert [g["org_id"] for g in got] == [scope.org_id, other.org_id, scope.org_id]
    assert got[0]["seq"] < got[1]["seq"] < got[2]["seq"]
    with role_tx(worker_engine, None) as conn:
        assert [g["id"] for g in fetch(conn, 2)] == [first, second]  # honours the limit


def test_fetch_rejects_a_bad_limit(worker_engine: Engine, migrated: None) -> None:
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, None) as conn:
        fetch(conn, 0)
    assert sqlstate(exc.value) == "22023"


def test_fetched_rows_stay_locked_until_the_callers_transaction_ends(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        event = insert_outbox(conn, scope)
    with role_tx(worker_engine, None) as conn:
        assert [g["id"] for g in fetch(conn, 5)] == [event]
        # Another session cannot lock or change the row while the fetching txn is open.
        with pytest.raises(OperationalError) as exc, admin_engine.begin() as other:
            other.execute(
                text("SELECT id FROM outbox_events WHERE id = :i FOR UPDATE NOWAIT"), {"i": event}
            )
        assert sqlstate(exc.value) == "55P03"
    # Committed without marking it: the row is available to the next fetch (at-least-once).
    with role_tx(worker_engine, None) as conn:
        assert [g["id"] for g in fetch(conn, 5)] == [event]


def test_two_concurrent_fetchers_get_disjoint_batches(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        ids = [insert_outbox(conn, scope) for _ in range(10)]
    with role_tx(worker_engine, None) as a:
        batch_a = [g["id"] for g in fetch(a, 5)]
        with role_tx(worker_engine, None) as b:
            batch_b = [g["id"] for g in fetch(b, 5)]
            # A is still open: B must not see A's rows, and vice versa the union is everything.
            assert set(batch_a).isdisjoint(batch_b)
            assert len(batch_a) == len(batch_b) == 5
            assert set(batch_a) | set(batch_b) == set(ids)
            with role_tx(worker_engine, None) as c:
                assert fetch(c, 5) == []  # everything is held by A or B


def test_concurrent_fetchers_never_overlap_under_load(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        ids = {insert_outbox(conn, scope) for _ in range(40)}
    barrier = threading.Barrier(4)
    seen: list[list[uuid.UUID]] = []
    lock = threading.Lock()

    def dispatcher() -> None:
        barrier.wait(timeout=20)
        while True:
            with role_tx(worker_engine, None) as conn:
                batch = [g["id"] for g in fetch(conn, 3)]
                if not batch:
                    return
                scalar(conn, "SELECT outbox_mark_dispatched(:i)", i=batch)
            with lock:
                seen.append(batch)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in [pool.submit(dispatcher) for _ in range(4)]:
            f.result(timeout=120)
    flat = [i for batch in seen for i in batch]
    assert len(flat) == len(set(flat)) == 40  # nothing twice, nothing lost
    assert set(flat) == ids


def test_mark_dispatched_marks_only_undispatched_rows_and_is_idempotent(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        a = insert_outbox(conn, scope)
        b = insert_outbox(conn, scope)
        done = insert_outbox(conn, scope, dispatched=True)
    with role_tx(worker_engine, None) as conn:
        assert scalar(conn, "SELECT outbox_mark_dispatched(:i)", i=[a, b, done]) == 2
    with role_tx(worker_engine, None) as conn:
        assert scalar(conn, "SELECT outbox_mark_dispatched(:i)", i=[a, b]) == 0
        assert fetch(conn, 10) == []
    with admin_engine.connect() as conn:
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM outbox_events WHERE id = ANY(:i) AND dispatched_at IS NULL",
                i=[a, b, done],
            )
            == 0
        )


def test_a_crash_before_commit_redelivers(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    """Send, then die before the commit: the mark rolls back and the row goes out again."""
    with admin_engine.begin() as conn:
        event = insert_outbox(conn, scope)
    with pytest.raises(RuntimeError, match="crash"), role_tx(worker_engine, None) as conn:
        assert [g["id"] for g in fetch(conn, 5)] == [event]
        scalar(conn, "SELECT outbox_mark_dispatched(:i)", i=[event])
        raise RuntimeError("crash")
    with role_tx(worker_engine, None) as conn:
        assert [g["id"] for g in fetch(conn, 5)] == [event]


def test_mark_failed_backs_off_and_records_the_error(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        event = insert_outbox(conn, scope)
        other = insert_outbox(conn, scope)
    with role_tx(worker_engine, None) as conn:
        assert scalar(conn, "SELECT outbox_mark_failed(:i, :e, 120)", i=event, e="x" * 900) is True
    with admin_engine.connect() as conn:
        row = rows(conn, "SELECT * FROM outbox_events WHERE id = :i", i=event)[0]
        delay = float(
            scalar(
                conn,
                "SELECT extract(epoch FROM available_at - now()) FROM outbox_events WHERE id = :i",
                i=event,
            )
        )
    assert row["attempts"] == 1
    assert row["last_error"] == "x" * 500  # truncated
    assert row["dispatched_at"] is None
    assert 100 < delay <= 120
    with role_tx(worker_engine, None) as conn:
        assert [g["id"] for g in fetch(conn, 10)] == [other]  # the failed row waits for its time
        assert scalar(conn, "SELECT outbox_mark_failed(:i, NULL, 0)", i=event) is True
    with role_tx(worker_engine, None) as conn:
        again = fetch(conn, 10)
    assert event in [g["id"] for g in again]
    assert [g["attempts"] for g in again if g["id"] == event] == [2]


def test_mark_failed_ignores_unknown_and_dispatched_rows(
    worker_engine: Engine, admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        done = insert_outbox(conn, scope, dispatched=True)
    with role_tx(worker_engine, None) as conn:
        assert scalar(conn, "SELECT outbox_mark_failed(:i, 'e', 5)", i=uuid.uuid4()) is False
        assert scalar(conn, "SELECT outbox_mark_failed(:i, 'e', 5)", i=done) is False


def test_mark_failed_rejects_a_negative_delay(worker_engine: Engine, migrated: None) -> None:
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, None) as conn:
        scalar(conn, "SELECT outbox_mark_failed(:i, 'e', -1)", i=uuid.uuid4())
    assert sqlstate(exc.value) == "22023"


# =============================================================================================
# knowledge_next_reconcile_batch
# =============================================================================================


def reconcile(conn: Any, limit: int = 10, older: int = 300) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM knowledge_next_reconcile_batch(:n, :o)", n=limit, o=older)


def published_scope(engine: Engine, *, reconciled_ago: int | None) -> tuple[Scope, uuid.UUID]:
    scope = seed_scope(engine)
    with engine.begin() as conn:
        rev = insert_revision(conn, scope, state="published")
        insert_publication(conn, scope, rev, reconciled_ago=reconciled_ago)
    return scope, rev


def test_reconcile_batch_returns_and_marks_stale_publications(
    worker_engine: Engine, admin_engine: Engine
) -> None:
    never, rev_never = published_scope(admin_engine, reconciled_ago=None)
    old, rev_old = published_scope(admin_engine, reconciled_ago=1000)
    recent, _ = published_scope(admin_engine, reconciled_ago=10)
    with role_tx(worker_engine, None) as conn:
        got = reconcile(conn, limit=10, older=300)
    assert [(g["org_id"], g["document_id"], g["revision_id"]) for g in got] == [
        (never.org_id, never.document_id, rev_never),  # never reconciled first
        (old.org_id, old.document_id, rev_old),
    ]
    assert set(got[0]) == {"org_id", "document_id", "revision_id"}
    # It marked what it returned: asking again gives nothing, and `recent` was never touched.
    with role_tx(worker_engine, None) as conn:
        assert reconcile(conn) == []
    with admin_engine.connect() as conn:
        marked = rows(
            conn,
            "SELECT document_id, extract(epoch FROM now() - reconciled_at) AS age"
            " FROM document_publications WHERE document_id = ANY(:d)",
            d=[never.document_id, old.document_id, recent.document_id],
        )
    ages = {m["document_id"]: float(m["age"]) for m in marked}
    assert ages[never.document_id] < 60
    assert ages[old.document_id] < 60
    assert ages[recent.document_id] == pytest.approx(10, abs=30)
    assert ages[recent.document_id] >= 9


def test_reconcile_batch_honours_the_limit_oldest_first(
    worker_engine: Engine, admin_engine: Engine
) -> None:
    newer, _ = published_scope(admin_engine, reconciled_ago=500)
    oldest, _ = published_scope(admin_engine, reconciled_ago=5000)
    middle, _ = published_scope(admin_engine, reconciled_ago=900)
    with role_tx(worker_engine, None) as conn:
        first = reconcile(conn, limit=2)
    assert [g["document_id"] for g in first] == [oldest.document_id, middle.document_id]
    with role_tx(worker_engine, None) as conn:
        second = reconcile(conn, limit=2)
    assert [g["document_id"] for g in second] == [newer.document_id]


def test_concurrent_reconcile_batches_are_disjoint(
    worker_engine: Engine, admin_engine: Engine
) -> None:
    docs = {published_scope(admin_engine, reconciled_ago=None)[0].document_id for _ in range(6)}
    with role_tx(worker_engine, None) as a:
        got_a = {g["document_id"] for g in reconcile(a, limit=3)}
        with role_tx(worker_engine, None) as b:
            got_b = {g["document_id"] for g in reconcile(b, limit=10)}
    assert len(got_a) == 3
    assert got_a.isdisjoint(got_b)
    assert got_a | got_b == docs


@pytest.mark.parametrize(("limit", "older"), [(0, 10), (10, -1)])
def test_reconcile_rejects_bad_arguments(
    worker_engine: Engine, migrated: None, limit: int, older: int
) -> None:
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, None) as conn:
        reconcile(conn, limit, older)
    assert sqlstate(exc.value) == "22023"
