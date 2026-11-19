"""M04.02.04: a worker that dies mid-run (SIGKILL). Real worker and dispatcher processes: the lease
lapses, the dispatcher's sweep reaps the run: ``failed lease_expired`` when tokens were already
streamed (a reconnecting client sees the log end, nothing is regenerated), requeued exactly once
when none were (the new worker runs it from the start without repeating a single event)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import redis
from catalog_support import World
from chat_support import (
    HERON,
    HERON_ANSWER,
    HERON_Q,
    ask_ok,
    chat_env,
    data_frames,
    drain_chat_backlog,
    end_reason,
    messages,
    model_calls,
    new_thread,
    read_sse,
    run_events,
    run_row,
    scenario,
    wait_run,
)
from compose_support import ComposeProject, wait_until
from ret_support import Rig
from runtime_support import Proc, start_dispatcher, start_worker
from sqlalchemy import Engine


@pytest.fixture(scope="module")
def dispatcher(
    stack: ComposeProject,
    fake_dir: Path,
    migrated: None,
    admin_engine: Engine,
    redis_client: redis.Redis,
) -> Iterator[dict[str, str]]:
    drain_chat_backlog(admin_engine, redis_client)
    env = chat_env(stack, fake_dir, DISPATCHER_HEALTH_FILE=str(fake_dir / "crash-dispatcher.alive"))
    proc = start_dispatcher(env, fake_dir, "crash-d")
    try:
        yield env
    finally:
        proc.stop()


def new_run(
    world: World, rig: Rig, fake_dir: Path, **script: Any
) -> tuple[uuid.UUID, uuid.UUID, str]:
    doc = rig.seed_world(world, title="Birds")
    rig.publish(world.org_id, world.ws, doc, [HERON])
    name = f"c{uuid.uuid4().hex[:10]}"
    token = scenario(fake_dir, name, **script)
    thread = new_thread(world.api, world.org_id, world.viewer)
    run = ask_ok(world.api, world.org_id, thread, world.viewer, f"{HERON_Q} {token}")
    return thread, run, name


def worker_for(env: dict[str, str], fake_dir: Path) -> Proc:
    return start_worker(env, fake_dir, concurrency=1, queues="chat")


def has_delta(world: World, run: uuid.UUID) -> bool:
    return any(e["type"] == "delta" for e in run_events(world.engine, run))


def test_sigkill_after_tokens_fails_lease_expired_and_nothing_is_regenerated(
    dispatcher: dict[str, str], world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    thread, run, scn = new_run(
        world, rig, fake_dir, text="Herons feed on fish. " * 40, chunk=3, delay=0.1
    )
    worker = worker_for(dispatcher, fake_dir)
    try:
        wait_until(
            lambda: has_delta(world, run), timeout=60, interval=0.1, description="first delta"
        )
        worker.kill9()
    finally:
        worker.stop()
    row = wait_run(world.engine, run, {"failed"}, timeout=60)  # lease 6 s + sweep 1 s
    assert row["error_code"] == "lease_expired"
    assert row["lease_owner"] is None
    events = run_events(world.engine, run)
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "failed"
    assert kinds.count("failed") == 1
    assert "completed" not in kinds
    assert events[-1]["data"] == {"error_code": "lease_expired"}
    assert kinds.count("started") == 1
    assert "delta" in kinds
    # a client that reconnects sees the log end with a user-safe failure, never a new answer
    _, _, frames = read_sse(
        live_url, world.org_id, run, world.viewer, last_event_id=events[0]["seq"]
    )
    last = data_frames(frames)[-1]
    assert last.event == "failed"
    assert last.data["error_code"] == "lease_expired"
    assert last.data["message"]
    assert end_reason(frames) == "terminal"
    assert [m["role"] for m in messages(world.engine, thread)] == ["user"]
    # a new worker does not pick the run up again
    fresh = worker_for(dispatcher, fake_dir)
    try:
        time.sleep(3)
        assert run_row(world.engine, run)["state"] == "failed"
        assert model_calls(fake_dir, scn) == 1
        assert [e["seq"] for e in run_events(world.engine, run)] == [e["seq"] for e in events]
    finally:
        fresh.stop()


def test_sigkill_before_the_first_token_requeues_once_without_repeating_events(
    dispatcher: dict[str, str], world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    thread, run, scn = new_run(
        world, rig, fake_dir, text=HERON_ANSWER, chunk=5, delay=2.5
    )  # 2.5 s before the first piece: a wide window to kill in
    worker = worker_for(dispatcher, fake_dir)
    try:
        wait_until(
            lambda: (
                [e["type"] for e in run_events(world.engine, run)][:2] == ["started", "retrieval"]
                and model_calls(fake_dir, scn) == 1
            ),
            timeout=60,
            interval=0.1,
            description="the model was asked",
        )
        assert not has_delta(world, run)
        worker.kill9()
    finally:
        worker.stop()
    wait_run(world.engine, run, {"queued"}, timeout=60)  # reaped and requeued (attempt 1 of 2)
    assert run_row(world.engine, run)["lease_owner"] is None
    fresh = worker_for(dispatcher, fake_dir)
    try:
        row = wait_run(world.engine, run, {"completed"}, timeout=90)
    finally:
        fresh.stop()
    assert row["attempts"] == 2
    assert row["error_code"] is None
    kinds = [e["type"] for e in run_events(world.engine, run)]
    assert kinds.count("started") == 1
    assert kinds.count("retrieval") == 1
    assert kinds[-3:] == ["citations", "verification", "completed"]
    assert kinds.count("completed") == 1
    text = "".join(e["data"]["text"] for e in run_events(world.engine, run) if e["type"] == "delta")
    assert text == HERON_ANSWER  # one answer, not two
    assert model_calls(fake_dir, scn) == 2  # asked again, but the first attempt streamed nothing
    assert [m["role"] for m in messages(world.engine, thread)] == ["user", "assistant"]
    _, _, frames = read_sse(live_url, world.org_id, run, world.viewer)
    assert [f.id for f in data_frames(frames)] == [e["seq"] for e in run_events(world.engine, run)]
