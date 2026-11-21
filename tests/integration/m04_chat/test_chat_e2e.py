"""M04.02.04 end to end: real PostgreSQL + Redis, a real Celery worker process and a real dispatcher
process, the real API served over HTTP, SSE read over a socket. Only the index (rebuilt from the
files the tests write) and the chat model (scripted) are fake; the gateway's policy check and every
other step is the production code.

Acceptance: reconnect never duplicates the run or its tokens."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from catalog_support import World
from chat_support import (
    HERON,
    HERON_ANSWER,
    HERON_Q,
    ask,
    ask_ok,
    base_path,
    data_frames,
    end_reason,
    messages,
    model_calls,
    new_thread,
    read_sse,
    run_events,
    run_row,
    scenario,
    streamed_text,
    wait_run,
)
from ret_support import Rig
from sqlalchemy import text

SLOW = HERON_ANSWER + " " + " ".join(["Herons feed on fish and frogs in shallow water [1]."] * 3)


def new_scn() -> str:
    return f"s{uuid.uuid4().hex[:10]}"


def setup_run(
    world: World,
    rig: Rig,
    fake_dir: Path,
    *,
    publish: bool = True,
    user: Any = None,
    **script: Any,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Publish a document, write a scenario and post a question; returns (thread, run, scn)."""
    if publish:
        doc = rig.seed_world(world, title="Birds")
        rig.publish(world.org_id, world.ws, doc, [HERON])
    name = new_scn()
    token = scenario(fake_dir, name, **{"text": HERON_ANSWER, "chunk": 4, **script})
    who = user or world.viewer
    thread = new_thread(world.api, world.org_id, who)
    run = ask_ok(world.api, world.org_id, thread, who, f"{HERON_Q} {token}")
    return thread, run, name


def test_happy_path_streams_everything_in_order(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    thread, run, scn = setup_run(world, rig, fake_dir, delay=0.02)
    status, headers, frames = read_sse(live_url, world.org_id, run, world.viewer)
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert frames[0].retry == 3000
    events = data_frames(frames)
    db = run_events(world.engine, run)
    assert [f.id for f in events] == [e["seq"] for e in db]  # exactly the stored log, in order
    kinds = [f.event for f in events]
    assert kinds[:2] == ["started", "retrieval"]
    assert kinds[-3:] == [
        "citations",
        "verification",
        "completed",
    ]
    assert set(kinds[2:-3]) == {"delta", "stage"}  # M11.07: stage "verifying" before citations
    assert kinds.count("delta") >= 2
    assert events[1].data["evidence_count"] >= 1
    assert events[1].data["degraded"] == []
    assert streamed_text(frames) == HERON_ANSWER
    citation = events[-3].data["citations"][0]
    assert citation["marker"] == "[1]"
    assert citation["workspace_id"] == str(world.ws)
    assert events[-2].data == {"status": "grounded", "withheld_count": 0, "flagged_count": 0}
    assert events[-1].data["answer_text"] == HERON_ANSWER
    assert end_reason(frames) == "terminal"
    row = wait_run(world.engine, run, {"completed"})
    assert row["attempts"] == 1
    assert [m["role"] for m in messages(world.engine, thread)] == ["user", "assistant"]
    assert model_calls(fake_dir, scn) == 1
    got = world.api.get(f"{base_path(world.org_id)}/runs/{run}", world.viewer).body
    assert got["state"] == "completed"
    assert got["message"]["content"] == HERON_ANSWER


def test_reconnect_never_duplicates_or_loses_tokens(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    thread, run, scn = setup_run(world, rig, fake_dir, text=SLOW, chunk=3, delay=0.04)
    org, user = world.org_id, world.viewer
    # the tab dies after a few events ...
    _, _, first = read_sse(live_url, org, run, user, stop_after=5)
    seen = data_frames(first)
    assert len(seen) == 5
    assert end_reason(first) is None
    cursor = seen[-1].id
    assert cursor is not None
    # ... and comes back with Last-Event-ID, dies again, comes back with ?last_event_id=
    _, _, second = read_sse(live_url, org, run, user, last_event_id=cursor, stop_after=4)
    middle = data_frames(second)
    assert middle[0].id is not None
    assert middle[0].id > cursor
    cursor2 = middle[-1].id
    _, _, third = read_sse(live_url, org, run, user, last_event_id=cursor2, query_last_id=True)
    tail = data_frames(third)
    assert end_reason(third) == "terminal"

    pieces = seen + middle + tail
    ids = [f.id for f in pieces]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)
    db = run_events(world.engine, run)
    assert ids == [e["seq"] for e in db]  # and none missing: exactly the stored log
    text_all = "".join(f.data["text"] for f in pieces if f.event == "delta")
    assert text_all == SLOW == "".join(e["data"]["text"] for e in db if e["type"] == "delta")
    assert pieces[-1].event == "completed"
    assert pieces[-1].data["answer_text"] == SLOW

    # a reconnect after completion replays everything; resuming at the end gets nothing new
    _, _, again = read_sse(live_url, org, run, user)
    assert [f.id for f in data_frames(again)] == ids
    assert end_reason(again) == "terminal"
    _, _, at_end = read_sse(live_url, org, run, user, last_event_id=ids[-1])
    assert data_frames(at_end) == []
    assert end_reason(at_end) == "terminal"
    # nothing was regenerated by any of the connections
    assert model_calls(fake_dir, scn) == 1
    assert run_row(world.engine, run)["attempts"] == 1
    assert len([m for m in messages(world.engine, thread) if m["role"] == "assistant"]) == 1


def test_idempotent_creation_runs_the_model_once(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    doc = rig.seed_world(world, title="Birds")
    rig.publish(world.org_id, world.ws, doc, [HERON])
    name = new_scn()
    token = scenario(fake_dir, name, text=HERON_ANSWER, chunk=5, delay=0.02)
    thread = new_thread(world.api, world.org_id, world.viewer)
    runs: list[uuid.UUID] = []
    barrier = threading.Barrier(6)

    def post() -> None:
        barrier.wait()
        res = ask(
            world.api,
            world.org_id,
            thread,
            world.viewer,
            f"{HERON_Q} {token}",
            key="one-key-for-all",
        )
        assert res.status in (200, 202), res.body
        runs.append(uuid.UUID(res.body["run_id"]))

    threads = [threading.Thread(target=post) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(set(runs)) == 1
    wait_run(world.engine, runs[0], {"completed"})
    time.sleep(1.5)  # a duplicate would have run by now
    assert model_calls(fake_dir, name) == 1
    assert [m["role"] for m in messages(world.engine, thread)] == ["user", "assistant"]


def test_cancel_mid_stream_over_the_real_worker(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    thread, run, scn = setup_run(
        world, rig, fake_dir, text="Herons feed on fish. " * 40, chunk=3, delay=0.08
    )
    result: dict[str, Any] = {}
    reader = threading.Thread(
        target=lambda: result.update(
            frames=read_sse(live_url, world.org_id, run, world.viewer, timeout=60)[2]
        )
    )
    reader.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(e["type"] == "delta" for e in run_events(world.engine, run)):
            break
        time.sleep(0.05)
    res = world.api.post(f"{base_path(world.org_id)}/runs/{run}/cancel", world.viewer, {})
    assert res.status == 202
    assert res.body == {"state": "cancelled"}
    reader.join(30)
    assert not reader.is_alive()
    frames = result["frames"]
    kinds = [f.event for f in data_frames(frames)]
    assert kinds[-1] == "cancelled"
    assert kinds.count("cancelled") == 1
    assert "completed" not in kinds
    assert "citations" not in kinds
    assert end_reason(frames) == "terminal"
    n = len(run_events(world.engine, run))
    time.sleep(1.0)
    assert len(run_events(world.engine, run)) == n  # the worker stopped writing
    assert run_row(world.engine, run)["state"] == "cancelled"
    # M11.07: a cancel after streamed text stores the partial answer as an assistant message.
    assert [m["role"] for m in messages(world.engine, thread)] == ["user", "assistant"]
    assert model_calls(fake_dir, scn) == 1


def test_duplicate_deliveries_of_the_message_run_once(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    _thread, run, scn = setup_run(world, rig, fake_dir, text=SLOW, chunk=4, delay=0.05)
    with world.engine.begin() as conn:
        for _ in range(3):  # three more deliveries of the same run, racing the first
            conn.execute(
                text(
                    "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type,"
                    " payload) VALUES (:o, 'chat_run', :r, 'chat.run.queued',"
                    " jsonb_build_object('run_id', CAST(:r AS text)))"
                ),
                {"o": world.org_id, "r": run},
            )
    wait_run(world.engine, run, {"completed"})
    time.sleep(2.0)  # let the duplicates be consumed
    kinds = [e["type"] for e in run_events(world.engine, run)]
    assert kinds.count("started") == 1
    assert kinds.count("completed") == 1
    assert run_row(world.engine, run)["attempts"] == 1
    assert model_calls(fake_dir, scn) == 1
    assert "nothing to claim" in runtime.worker.text()


def test_no_evidence_abstains_and_the_model_is_never_called(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    _thread, run, scn = setup_run(world, rig, fake_dir, publish=False)
    _, _, frames = read_sse(live_url, world.org_id, run, world.viewer)
    kinds = [f.event for f in data_frames(frames)]
    assert kinds == ["started", "retrieval", "citations", "verification", "completed"]
    assert data_frames(frames)[-2].data["status"] == "abstained"
    assert model_calls(fake_dir, scn) == 0


def test_policy_denied_over_the_real_worker(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    res = world.api.put(
        f"/v1/orgs/{world.org_id}/ai-policy",
        world.t.owner,
        {"allow_external": True, "allowed_providers": ["openai"]},
    )
    assert res.status == 200
    _thread, run, scn = setup_run(world, rig, fake_dir)
    _, _, frames = read_sse(live_url, world.org_id, run, world.viewer)
    failed = data_frames(frames)[-1]
    assert failed.event == "failed"
    assert failed.data["error_code"] == "policy_denied"
    assert failed.data["message"]
    assert "openai" not in failed.data["message"].lower()
    assert end_reason(frames) == "terminal"
    assert model_calls(fake_dir, scn) == 0
    assert run_row(world.engine, run)["error_code"] == "policy_denied"


def test_model_errors_before_and_after_the_first_token(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    doc = rig.seed_world(world, title="Birds")
    rig.publish(world.org_id, world.ws, doc, [HERON])
    _, early, _ = setup_run(world, rig, fake_dir, publish=False, error_after=0)
    _, _, frames = read_sse(live_url, world.org_id, early, world.viewer)
    assert [f.event for f in data_frames(frames)] == ["started", "retrieval", "failed"]
    assert data_frames(frames)[-1].data["error_code"] == "model_unavailable"

    _, late, _ = setup_run(world, rig, fake_dir, publish=False, error_after=2)
    _, _, frames2 = read_sse(live_url, world.org_id, late, world.viewer)
    kinds = [f.event for f in data_frames(frames2)]
    assert kinds[-1] == "failed"
    assert "delta" in kinds
    assert "completed" not in kinds
    assert streamed_text(frames2) == HERON_ANSWER[:8]  # the two pieces written before the error
    assert run_row(world.engine, late)["error_code"] == "model_unavailable"


def test_security_over_the_live_stream(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    _thread, run, _ = setup_run(world, rig, fake_dir, text=SLOW, chunk=3, delay=0.05)
    for who in (world.other_plain, world.t.admin, world.t.outsider, world.foreign.owner):
        assert read_sse(live_url, world.org_id, run, who)[0] == 404
        assert world.api.post(f"{base_path(world.org_id)}/runs/{run}/cancel", who, {}).status == 404
    # the run is untouched by the attempts above
    wait_run(world.engine, run, {"completed"})


def test_no_text_in_process_logs(
    runtime: Any, world: World, rig: Rig, fake_dir: Path, live_url: str
) -> None:
    doc = rig.seed_world(world, title="Birds")
    rig.publish(world.org_id, world.ws, doc, [HERON + " Hidden marker QUOKKA-EVIDENCE-6021."])
    name = new_scn()
    token = scenario(fake_dir, name, text=HERON_ANSWER + " QUOKKA-ANSWER-4410", chunk=4)
    thread = new_thread(world.api, world.org_id, world.viewer)
    run = ask_ok(
        world.api, world.org_id, thread, world.viewer, f"{HERON_Q} QUOKKA-QUESTION-7733 {token}"
    )
    wait_run(world.engine, run, {"completed"})
    time.sleep(1.0)
    for log in (runtime.worker.text(), runtime.dispatcher.text()):
        for secret in (
            "QUOKKA-QUESTION-7733",
            "QUOKKA-EVIDENCE-6021",
            "QUOKKA-ANSWER-4410",
            HERON_Q,
        ):
            assert secret not in log, secret
