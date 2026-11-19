"""M04.02.04 persistence: atomic run creation, idempotency, privacy, claim, fencing, reaping."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from m04_support import ChatWorld, rows, scalar
from sqlalchemy import Engine, text

from app.core.db import tenant_session
from app.modules.ai import chat_repo as repo
from app.workers.worker_db import system_connection


def _tx(engine: Engine, user: uuid.UUID, org: uuid.UUID) -> Any:
    return tenant_session(user_id=user, org_id=org, engine=engine)


def _thread(app_engine: Engine, w: ChatWorld, user: uuid.UUID | None = None) -> repo.ThreadRow:
    with _tx(app_engine, user or w.member, w.org) as s:
        return repo.create_thread(s, org_id=w.org, user_id=user or w.member, workspace_id=w.ws)


def _run(
    app_engine: Engine,
    w: ChatWorld,
    thread: repo.ThreadRow,
    key: str | None = None,
    **kw: Any,
) -> repo.CreatedRun:
    with _tx(app_engine, thread.created_by, w.org) as s:
        return repo.create_run(
            s,
            org_id=w.org,
            user_id=thread.created_by,
            thread_id=thread.id,
            content="What is the refund policy?",
            idempotency_key=key or uuid.uuid4().hex,
            scope_snapshot={"workspace_ids": [str(w.ws)]},
            **kw,
        )


def _claim(
    worker_engine: Engine, run_id: uuid.UUID, owner: str = "w1", lease: int = 60
) -> repo.ClaimedRun | None:
    with system_connection(worker_engine) as conn:
        return repo.claim_run(conn, run_id, owner, lease)


def _expire(admin_engine: Engine, run_id: uuid.UUID) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE chat_runs SET lease_expires_at = now() - interval '1 minute' WHERE id = :r"
            ),
            {"r": run_id},
        )


def _reap(worker_engine: Engine, max_attempts: int = 2) -> list[repo.ReapedRun]:
    with system_connection(worker_engine) as conn:
        return repo.reap_expired_runs(conn, max_attempts)


def _state(admin_engine: Engine, run_id: uuid.UUID) -> dict[str, Any]:
    return rows(admin_engine, "SELECT * FROM chat_runs WHERE id = :r", r=run_id)[0]


# ------------------------------------------------------------------------- creation and outbox


def test_create_run_writes_run_message_and_outbox_atomically(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    thread = _thread(app_engine, world)
    created = _run(app_engine, world, thread)
    assert created.created
    run = _state(admin_engine, created.run_id)
    assert (run["state"], run["attempts"], run["lease_epoch"]) == ("queued", 0, 0)
    assert run["created_by"] == world.member
    assert run["user_message_id"] == created.message_id
    assert run["scope_snapshot"] == {"workspace_ids": [str(world.ws)]}
    msg = rows(admin_engine, "SELECT * FROM chat_messages WHERE id = :m", m=created.message_id)[0]
    assert (msg["role"], msg["run_id"], msg["thread_id"]) == ("user", created.run_id, thread.id)
    box = rows(
        admin_engine, "SELECT * FROM outbox_events WHERE aggregate_id = :a", a=created.run_id
    )
    assert len(box) == 1
    assert box[0]["event_type"] == "chat.run.queued"
    assert box[0]["aggregate_type"] == "chat_run"
    assert box[0]["payload"] == {"run_id": str(created.run_id)}
    assert box[0]["dispatched_at"] is None


def test_rolled_back_creation_leaves_nothing(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    thread = _thread(app_engine, world)
    key = uuid.uuid4().hex
    with pytest.raises(RuntimeError), _tx(app_engine, world.member, world.org) as s:
        repo.create_run(
            s,
            org_id=world.org,
            user_id=world.member,
            thread_id=thread.id,
            content="x",
            idempotency_key=key,
            scope_snapshot={},
        )
        raise RuntimeError("boom")
    assert (
        scalar(admin_engine, "SELECT count(*) FROM chat_runs WHERE idempotency_key = :k", k=key)
        == 0
    )
    assert (
        scalar(admin_engine, "SELECT count(*) FROM chat_messages WHERE thread_id = :t", t=thread.id)
        == 0
    )


def test_same_key_returns_the_same_run(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    thread = _thread(app_engine, world)
    first = _run(app_engine, world, thread, key="k-1")
    again = _run(app_engine, world, thread, key="k-1")
    assert (again.run_id, again.message_id, again.created) == (
        first.run_id,
        first.message_id,
        False,
    )
    assert (
        scalar(admin_engine, "SELECT count(*) FROM chat_runs WHERE thread_id = :t", t=thread.id)
        == 1
    )
    assert (
        scalar(admin_engine, "SELECT count(*) FROM chat_messages WHERE thread_id = :t", t=thread.id)
        == 1
    )
    assert (
        scalar(
            admin_engine,
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = :a",
            a=first.run_id,
        )
        == 1
    )


def test_concurrent_submissions_of_one_key_create_one_run(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    thread = _thread(app_engine, world)
    barrier = threading.Barrier(8)

    def submit(_: int) -> repo.CreatedRun:
        barrier.wait()
        return _run(app_engine, world, thread, key="race")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(submit, range(8)))
    assert len({r.run_id for r in results}) == 1
    assert len({r.message_id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert (
        scalar(admin_engine, "SELECT count(*) FROM chat_runs WHERE thread_id = :t", t=thread.id)
        == 1
    )
    assert (
        scalar(admin_engine, "SELECT count(*) FROM chat_messages WHERE thread_id = :t", t=thread.id)
        == 1
    )
    assert (
        scalar(
            admin_engine,
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = :a",
            a=results[0].run_id,
        )
        == 1
    )


def test_concurrent_submissions_with_limits_create_one_run(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    thread = _thread(app_engine, world)
    barrier = threading.Barrier(6)

    def submit(_: int) -> repo.CreatedRun:
        barrier.wait()
        return _run(
            app_engine, world, thread, key="race-limited", max_concurrent=5, max_per_minute=50
        )

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(submit, range(6)))
    assert len({r.run_id for r in results}) == 1
    assert sum(r.created for r in results) == 1


def test_key_reuse_in_another_thread_or_by_another_user_never_returns_that_run(
    app_engine: Engine, world: ChatWorld
) -> None:
    t1, t2 = _thread(app_engine, world), _thread(app_engine, world)
    a = _run(app_engine, world, t1, key="shared")
    b = _run(app_engine, world, t2, key="shared")
    assert a.run_id != b.run_id
    # Another member cannot even address the thread, so the key cannot leak its run.
    with pytest.raises(repo.ThreadNotFound), _tx(app_engine, world.member2, world.org) as s:
        repo.create_run(
            s,
            org_id=world.org,
            user_id=world.member2,
            thread_id=t1.id,
            content="x",
            idempotency_key="shared",
            scope_snapshot={},
        )


# ------------------------------------------------------------------------------------ privacy


def test_threads_are_private_to_their_creator(app_engine: Engine, world: ChatWorld) -> None:
    thread = _thread(app_engine, world)
    created = _run(app_engine, world, thread)
    for other in (world.member2, world.admin, world.owner):
        with _tx(app_engine, other, world.org) as s:
            assert repo.get_thread(s, org_id=world.org, user_id=other, thread_id=thread.id) is None
            assert thread.id not in {
                t.id for t in repo.list_threads(s, org_id=world.org, user_id=other)[0]
            }
            # Even forged "as the creator" filters find nothing: RLS decides, not the filter.
            assert repo.list_messages(s, org_id=world.org, thread_id=thread.id) == []
            assert (
                repo.get_run(s, org_id=world.org, user_id=world.member, run_id=created.run_id)
                is None
            )
            assert repo.cancel_run(s, created.run_id) is None
            assert repo.read_events_after(s, org_id=world.org, run_id=created.run_id) == []
            assert repo.count_active_runs(s, user_id=world.member) == 0
    with _tx(app_engine, world.member, world.org) as s:
        assert repo.get_thread(s, org_id=world.org, user_id=world.member, thread_id=thread.id)
        assert len(repo.list_messages(s, org_id=world.org, thread_id=thread.id)) == 1


def test_another_org_cannot_see_or_open_the_thread(app_engine: Engine, world: ChatWorld) -> None:
    thread = _thread(app_engine, world)
    created = _run(app_engine, world, thread)
    with _tx(app_engine, world.owner_b, world.org_b) as s:
        assert (
            repo.get_thread(s, org_id=world.org_b, user_id=world.owner_b, thread_id=thread.id)
            is None
        )
        assert repo.cancel_run(s, created.run_id) is None
    # Same user id, wrong org context: still nothing.
    with _tx(app_engine, world.member, world.org_b) as s:
        assert (
            repo.get_thread(s, org_id=world.org_b, user_id=world.member, thread_id=thread.id)
            is None
        )
        assert repo.cancel_run(s, created.run_id) is None


def test_list_threads_pages_without_duplicates(app_engine: Engine, world: ChatWorld) -> None:
    user = world.member2
    made = [_thread(app_engine, world, user).id for _ in range(5)]
    seen: list[uuid.UUID] = []
    cursor = None
    with _tx(app_engine, user, world.org) as s:
        for _ in range(10):
            page, cursor = repo.list_threads(
                s, org_id=world.org, user_id=user, limit=2, after=cursor
            )
            seen.extend(t.id for t in page)
            if cursor is None:
                break
    assert [t for t in seen if t in made] == seen[: len(seen)]
    assert len(seen) == len(set(seen))
    assert set(made) <= set(seen)


def test_soft_delete_hides_the_thread_and_cancels_active_runs(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    thread = _thread(app_engine, world)
    created = _run(app_engine, world, thread)
    with _tx(app_engine, world.member2, world.org) as s:
        assert not repo.soft_delete_thread(
            s, org_id=world.org, user_id=world.member2, thread_id=thread.id
        )
    with _tx(app_engine, world.member, world.org) as s:
        assert repo.soft_delete_thread(
            s, org_id=world.org, user_id=world.member, thread_id=thread.id
        )
    with _tx(app_engine, world.member, world.org) as s:
        assert (
            repo.get_thread(s, org_id=world.org, user_id=world.member, thread_id=thread.id) is None
        )
        assert not repo.soft_delete_thread(
            s, org_id=world.org, user_id=world.member, thread_id=thread.id
        )
    assert _state(admin_engine, created.run_id)["state"] == "cancelled"
    # No new run in a deleted thread, neither through the repository nor by direct SQL.
    with pytest.raises(repo.ThreadNotFound):
        _run(app_engine, world, thread)
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError), _tx(app_engine, world.member, world.org) as s:
        s.execute(
            text(
                "INSERT INTO chat_messages (id, org_id, thread_id, role, content)"
                " VALUES (:i, :o, :t, 'user', 'x')"
            ),
            {"i": uuid.uuid4(), "o": world.org, "t": thread.id},
        )


def test_rate_limits_count_per_user_and_spare_replays(app_engine: Engine, world: ChatWorld) -> None:
    user = world.admin
    thread = _thread(app_engine, world, user)
    _run(app_engine, world, thread, key="a", max_concurrent=2)
    _run(app_engine, world, thread, key="b", max_concurrent=2)
    with pytest.raises(repo.RunLimitExceeded) as exc:
        _run(app_engine, world, thread, key="c", max_concurrent=2)
    assert exc.value.kind == "concurrent"
    again = _run(app_engine, world, thread, key="a", max_concurrent=2)  # a replay is not a new run
    assert not again.created
    with _tx(app_engine, user, world.org) as s:
        assert repo.count_active_runs(s, user_id=user) == 2
        assert repo.count_runs_since(s, user_id=user, seconds=60) == 2
    with pytest.raises(repo.RunLimitExceeded) as per_min:
        _run(app_engine, world, thread, key="d", max_per_minute=2)
    assert per_min.value.kind == "per_minute"


# ---------------------------------------------------------------------------------- cancelling


def test_creator_cancels_a_queued_run_and_it_cannot_be_claimed(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    created = _run(app_engine, world, _thread(app_engine, world))
    with _tx(app_engine, world.member, world.org) as s:
        res = repo.cancel_run(s, created.run_id)
    assert res == repo.CancelResult(created.run_id, "cancelled", True)
    with _tx(app_engine, world.member, world.org) as s:
        assert repo.cancel_run(s, created.run_id) == repo.CancelResult(
            created.run_id, "cancelled", False
        )
        events = repo.read_events_after(s, org_id=world.org, run_id=created.run_id)
    assert [e.type for e in events] == ["cancelled"]
    run = _state(admin_engine, created.run_id)
    assert run["finished_at"] is not None
    assert run["lease_owner"] is None
    assert _claim(worker_engine, created.run_id) is None


def test_cancel_fences_out_a_running_worker(
    app_engine: Engine, worker_engine: Engine, world: ChatWorld
) -> None:
    created = _run(app_engine, world, _thread(app_engine, world))
    claim = _claim(worker_engine, created.run_id)
    assert claim is not None
    with repo.worker_chat_session(
        worker_engine, org_id=claim.org_id, user_id=claim.created_by
    ) as s:
        repo.append_event(s, claim.fence, type="started", data={"model": "m"})
    with _tx(app_engine, world.member, world.org) as s:
        assert repo.cancel_run(s, created.run_id).cancelled  # type: ignore[union-attr]
    with repo.worker_chat_session(
        worker_engine, org_id=claim.org_id, user_id=claim.created_by
    ) as s:
        with pytest.raises(repo.RunFenceLost):
            repo.append_event(s, claim.fence, type="delta", data={"text": "x"})
        with pytest.raises(repo.RunFenceLost):
            repo.finish_run(s, claim.fence, state="completed")
        with pytest.raises(repo.RunFenceLost):
            repo.renew_lease(s, claim.fence, 60)


# ------------------------------------------------------------------------------------- claiming


def test_claim_is_exactly_once_under_concurrency(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    created = _run(app_engine, world, _thread(app_engine, world))
    barrier = threading.Barrier(10)

    def attempt(i: int) -> repo.ClaimedRun | None:
        barrier.wait()
        return _claim(worker_engine, created.run_id, owner=f"w{i}")

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(attempt, range(10)))
    won = [r for r in results if r is not None]
    assert len(won) == 1
    claim = won[0]
    assert (claim.attempts, claim.lease_epoch) == (1, 1)
    assert (claim.org_id, claim.created_by) == (world.org, world.member)
    assert claim.user_message_id == created.message_id
    run = _state(admin_engine, created.run_id)
    assert (run["state"], run["lease_owner"], run["lease_epoch"]) == (
        "running",
        claim.lease_owner,
        1,
    )
    assert run["lease_expires_at"] is not None
    assert run["started_at"] is not None
    assert _claim(worker_engine, created.run_id, owner="late") is None  # already running


def test_claim_of_an_unknown_run_or_bad_arguments(worker_engine: Engine) -> None:
    assert _claim(worker_engine, uuid.uuid4()) is None
    with pytest.raises(Exception, match="positive lease"):
        _claim(worker_engine, uuid.uuid4(), lease=0)


# ------------------------------------------------------------------------- fenced writes / run


def test_full_run_lifecycle_with_fenced_writes(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    thread = _thread(app_engine, world)
    created = _run(app_engine, world, thread)
    claim = _claim(worker_engine, created.run_id)
    assert claim is not None
    with repo.worker_chat_session(
        worker_engine, org_id=claim.org_id, user_id=claim.created_by
    ) as s:
        repo.set_run_model(
            s, claim.fence, {"provider": "local", "model_id": "m", "prompt_version": "1"}
        )
        seqs = [
            repo.append_event(s, claim.fence, type="started"),
            repo.append_event(s, claim.fence, type="delta", data={"text": "Refunds "}),
            repo.append_event(s, claim.fence, type="delta", data={"text": "take 30 days [1]."}),
        ]
        repo.renew_lease(s, claim.fence, 120)
        msg = repo.append_assistant_message(
            s,
            claim.fence,
            thread_id=thread.id,
            content="Refunds take 30 days [1].",
            citations=[{"marker": "[1]", "chunk_id": "c1"}],
            verification={"status": "grounded"},
        )
        repo.append_event(s, claim.fence, type="completed", data={"message_id": str(msg)})
        repo.finish_run(s, claim.fence, state="completed")
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3
    run = _state(admin_engine, created.run_id)
    assert (run["state"], run["lease_owner"], run["error_code"]) == ("completed", None, None)
    assert run["finished_at"] is not None
    assert run["model"]["model_id"] == "m"
    with _tx(app_engine, world.member, world.org) as s:
        history = repo.list_messages(s, org_id=world.org, thread_id=thread.id)
        events = repo.read_events_after(s, org_id=world.org, run_id=created.run_id)
        later = repo.read_events_after(
            s, org_id=world.org, run_id=created.run_id, after_seq=seqs[1]
        )
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[1].citations == [{"marker": "[1]", "chunk_id": "c1"}]
    assert history[1].verification == {"status": "grounded"}
    assert [e.type for e in events] == ["started", "delta", "delta", "completed"]
    assert [e.type for e in later] == ["delta", "completed"]
    # A finished run accepts no more writes.
    with repo.worker_chat_session(
        worker_engine, org_id=claim.org_id, user_id=claim.created_by
    ) as s:
        with pytest.raises(repo.RunFenceLost):
            repo.append_event(s, claim.fence, type="delta")
        with pytest.raises(repo.RunFenceLost):
            repo.append_assistant_message(s, claim.fence, thread_id=thread.id, content="again")


def test_a_stale_epoch_is_rejected_after_a_requeue_and_reclaim(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    created = _run(app_engine, world, _thread(app_engine, world))
    old = _claim(worker_engine, created.run_id, owner="old")
    assert old is not None
    _expire(admin_engine, created.run_id)
    assert [(r.run_id, r.outcome) for r in _reap(worker_engine)] == [(created.run_id, "requeued")]
    assert (
        scalar(
            admin_engine,
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = :a"
            " AND event_type = 'chat.run.queued'",
            a=created.run_id,
        )
        == 2
    )
    new = _claim(worker_engine, created.run_id, owner="new")
    assert new is not None
    assert (new.lease_epoch, new.attempts) == (2, 2)
    with repo.worker_chat_session(worker_engine, org_id=world.org, user_id=world.member) as s:
        with pytest.raises(repo.RunFenceLost):
            repo.append_event(s, old.fence, type="delta", data={"text": "stale"})
        # same owner, wrong epoch
        with pytest.raises(repo.RunFenceLost):
            repo.append_event(s, repo.RunFence(old.run_id, old.org_id, "new", 1), type="delta")
        # right epoch, wrong owner
        with pytest.raises(repo.RunFenceLost):
            repo.append_event(s, repo.RunFence(old.run_id, old.org_id, "old", 2), type="delta")
        repo.append_event(s, new.fence, type="started")
    assert (
        scalar(
            admin_engine, "SELECT count(*) FROM chat_run_events WHERE run_id = :r", r=created.run_id
        )
        == 1
    )


# --------------------------------------------------------------------------------------- reaper


def test_reaper_requeues_once_then_fails(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    created = _run(app_engine, world, _thread(app_engine, world))
    assert _claim(worker_engine, created.run_id) is not None
    assert _reap(worker_engine) == []  # lease still valid
    _expire(admin_engine, created.run_id)
    assert [r.outcome for r in _reap(worker_engine)] == ["requeued"]
    run = _state(admin_engine, created.run_id)
    assert (run["state"], run["lease_owner"], run["attempts"]) == ("queued", None, 1)
    assert _claim(worker_engine, created.run_id) is not None  # second attempt
    _expire(admin_engine, created.run_id)
    assert [r.outcome for r in _reap(worker_engine)] == ["failed"]  # attempts used up
    run = _state(admin_engine, created.run_id)
    assert (run["state"], run["error_code"]) == ("failed", "lease_expired")
    assert run["finished_at"] is not None
    assert run["lease_owner"] is None
    with _tx(app_engine, world.member, world.org) as s:
        events = repo.read_events_after(s, org_id=world.org, run_id=created.run_id)
    assert events[-1].type == "failed"
    assert events[-1].data == {"error_code": "lease_expired"}


def test_reaper_fails_a_run_that_already_streamed_tokens(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    created = _run(app_engine, world, _thread(app_engine, world))
    claim = _claim(worker_engine, created.run_id)
    assert claim is not None
    with repo.worker_chat_session(
        worker_engine, org_id=claim.org_id, user_id=claim.created_by
    ) as s:
        repo.append_event(s, claim.fence, type="started")
        repo.append_event(s, claim.fence, type="delta", data={"text": "partial"})
    _expire(admin_engine, created.run_id)
    assert [r.outcome for r in _reap(worker_engine)] == [
        "failed"
    ]  # attempts 1 < 2, but tokens went out
    assert _state(admin_engine, created.run_id)["error_code"] == "lease_expired"
    assert _claim(worker_engine, created.run_id) is None
    with pytest.raises(Exception, match="max attempts"):
        _reap(worker_engine, 0)


def test_reaper_ignores_queued_and_finished_runs(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    queued = _run(app_engine, world, _thread(app_engine, world))
    cancelled = _run(app_engine, world, _thread(app_engine, world))
    with _tx(app_engine, world.member, world.org) as s:
        repo.cancel_run(s, cancelled.run_id)
    _expire(admin_engine, queued.run_id)
    _expire(admin_engine, cancelled.run_id)
    assert all(r.run_id not in (queued.run_id, cancelled.run_id) for r in _reap(worker_engine))


# ---------------------------------------------------------------------------- scope definer


def test_readable_workspaces_follow_the_scope_derivation(
    app_engine: Engine, worker_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    ws2 = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
                " VALUES (:w, :o, :s, 'WS2', :u)"
            ),
            {"w": ws2, "o": world.org, "s": f"ws-{uuid.uuid4().hex[:10]}", "u": world.owner},
        )

    def readable(user: uuid.UUID, org: uuid.UUID | None = None) -> frozenset[uuid.UUID]:
        with system_connection(worker_engine) as conn:
            return repo.readable_workspaces(conn, org or world.org, user)

    assert readable(world.owner) == {world.ws, ws2}
    assert readable(world.admin) == {world.ws, ws2}
    assert readable(world.member2) == frozenset()
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
                " VALUES (:w, :o, :u, 'viewer')"
            ),
            {"w": ws2, "o": world.org, "u": world.member2},
        )
    assert readable(world.member2) == {ws2}
    assert readable(world.owner_b) == frozenset()  # not a member of org A
    assert readable(world.owner, world.org_b) == frozenset()  # org A's owner in org B
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE workspaces SET deleted_at = now() WHERE id = :w"), {"w": ws2})
    assert readable(world.member2) == frozenset()
    assert readable(world.owner) == {world.ws}
