"""M04.02.04: the chat API on a real database (no worker: runs stay ``queued``): threads, idempotent
run creation, limits, cancel, privacy (404 for everyone but the creator), the SSE protocol of a run
that already ended, and the OpenAPI document."""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from catalog_support import World
from chat_support import (
    ask,
    ask_ok,
    base_path,
    data_frames,
    end_reason,
    event_types,
    messages,
    new_thread,
    read_sse,
    run_events,
    run_row,
)
from fastapi import FastAPI
from sqlalchemy import Engine, text

Q = "What do herons feed on? SECRET-QUESTION-TEXT-7731"


def audit_rows(admin: Engine, event_type: str, target: uuid.UUID | str) -> list[dict[str, Any]]:
    with admin.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT * FROM audit_events WHERE event_type = :t AND target_id = :i"
                    " ORDER BY occurred_at"
                ),
                {"t": event_type, "i": str(target)},
            ).mappings()
        ]


def outbox_count(admin: Engine, run_id: uuid.UUID) -> int:
    with admin.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE aggregate_id = :r"
                    " AND event_type = 'chat.run.queued'"
                ),
                {"r": run_id},
            ).scalar_one()
        )


# ----------------------------------------------------------------------------------- threads


def test_thread_lifecycle(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    res = api.post(f"{base_path(org)}/threads", owner, {"title": "  First  "})
    assert res.status == 201, res.body
    assert set(res.body) == {"id", "workspace_id", "title", "created_at", "updated_at"}
    assert res.body["title"] == "First"
    assert res.body["workspace_id"] is None
    tid = res.body["id"]

    detail = api.get(f"{base_path(org)}/threads/{tid}", owner)
    assert detail.status == 200
    assert set(detail.body) == {"thread", "messages", "active_run"}
    assert detail.body["messages"] == []
    assert detail.body["active_run"] is None

    renamed = api.patch(f"{base_path(org)}/threads/{tid}", owner, {"title": "Renamed"})
    assert renamed.status == 200
    assert renamed.body["title"] == "Renamed"
    assert api.patch(f"{base_path(org)}/threads/{tid}", owner, {"title": "x" * 201}).status == 422

    assert api.delete(f"{base_path(org)}/threads/{tid}", owner).status == 204
    assert api.get(f"{base_path(org)}/threads/{tid}", owner).status == 404
    assert api.delete(f"{base_path(org)}/threads/{tid}", owner).status == 404
    assert audit_rows(world.engine, "chat.thread.deleted", tid)


def test_thread_list_is_own_newest_first_with_keyset(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    ids = [new_thread(api, org, owner, title=f"t{i}") for i in range(5)]
    theirs = new_thread(api, org, world.t.admin, title="admin's")
    first = api.get(f"{base_path(org)}/threads", owner, params={"limit": 2})
    assert first.status == 200
    assert [i["id"] for i in first.body["items"]] == [str(ids[4]), str(ids[3])]
    assert first.body["next_cursor"]
    second = api.get(
        f"{base_path(org)}/threads", owner, params={"limit": 2, "after": first.body["next_cursor"]}
    )
    assert [i["id"] for i in second.body["items"]] == [str(ids[2]), str(ids[1])]
    third = api.get(
        f"{base_path(org)}/threads", owner, params={"limit": 2, "after": second.body["next_cursor"]}
    )
    assert [i["id"] for i in third.body["items"]] == [str(ids[0])]
    assert third.body["next_cursor"] is None
    everything = {
        i["id"]
        for i in api.get(f"{base_path(org)}/threads", owner, params={"limit": 100}).body["items"]
    }
    assert str(theirs) not in everything  # another member's thread is not listed
    assert api.get(f"{base_path(org)}/threads", owner, params={"limit": 0}).status == 422
    assert api.get(f"{base_path(org)}/threads", owner, params={"limit": 101}).status == 422
    assert api.get(f"{base_path(org)}/threads", owner, params={"after": "nope"}).status == 422


def test_thread_workspace_must_be_readable(world: World) -> None:
    api, org = world.api, world.org_id
    ok = api.post(f"{base_path(org)}/threads", world.viewer, {"workspace_id": str(world.ws)})
    assert ok.status == 201
    assert ok.body["workspace_id"] == str(world.ws)
    # an org member without any role in that workspace, and a workspace of another org: concealed
    assert (
        api.post(f"{base_path(org)}/threads", world.plain, {"workspace_id": str(world.ws)}).status
        == 404
    )
    assert (
        api.post(
            f"{base_path(org)}/threads", world.t.owner, {"workspace_id": str(world.foreign_ws)}
        ).status
        == 404
    )


def test_threads_and_runs_are_private_to_their_creator(world: World) -> None:
    api, org = world.api, world.org_id
    owner, other, outsider = world.t.owner, world.other_plain, world.t.outsider
    tid = new_thread(api, org, owner)
    run = ask_ok(api, org, tid, owner, Q)
    for who in (other, world.t.admin, outsider, world.foreign.owner):
        t_url = f"{base_path(org)}/threads/{tid}"
        r_url = f"{base_path(org)}/runs/{run}"
        assert api.get(t_url, who).status == 404
        assert api.patch(t_url, who, {"title": "x"}).status == 404
        assert api.delete(t_url, who).status == 404
        assert ask(api, org, tid, who, Q).status == 404
        assert api.get(r_url, who).status == 404
        assert api.post(f"{r_url}/cancel", who, {}).status == 404
    assert api.get(f"{base_path(org)}/threads", None).status == 401
    # still intact for the creator
    assert api.get(f"{base_path(org)}/runs/{run}", owner).body["state"] == "queued"


def test_sse_is_private_too(world: World, live_url: str) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    run = ask_ok(api, org, tid, owner, Q)
    api.post(f"{base_path(org)}/runs/{run}/cancel", owner, {})
    for who in (world.other_plain, world.t.admin, world.t.outsider, world.foreign.owner):
        status, _, frames = read_sse(live_url, org, run, who)
        assert status == 404
        assert frames == []
    assert read_sse(live_url, org, run, None)[0] == 401
    assert read_sse(live_url, org, uuid.uuid4(), owner)[0] == 404


# --------------------------------------------------------------------------- run creation


def test_create_run_shape_outbox_audit_and_no_text(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    res = ask(api, org, tid, owner, Q, key="idem-key-0001")
    assert res.status == 202, res.body
    assert set(res.body) == {"run_id", "message_id", "state"}
    assert res.body["state"] == "queued"
    run = uuid.UUID(res.body["run_id"])
    row = run_row(world.engine, run)
    assert row["state"] == "queued"
    assert row["created_by"] == owner.id
    assert row["scope_snapshot"]["workspace_ids"] == sorted({str(world.ws)})
    assert outbox_count(world.engine, run) == 1
    msgs = messages(world.engine, tid)
    assert [(m["role"], m["content"]) for m in msgs] == [("user", Q)]
    assert str(msgs[0]["id"]) == res.body["message_id"]
    audit = audit_rows(world.engine, "chat.run.created", run)
    assert len(audit) == 1
    blob = json.dumps(audit[0], default=str)
    assert "SECRET-QUESTION-TEXT-7731" not in blob
    assert "idem-key-0001" not in blob
    detail = api.get(f"{base_path(org)}/threads/{tid}", owner)
    assert detail.body["active_run"] == {"run_id": str(run), "state": "queued"}
    assert [m["role"] for m in detail.body["messages"]] == ["user"]
    assert detail.body["messages"][0]["run_id"] == str(run)
    assert detail.body["messages"][0]["citations"] == []
    assert detail.body["messages"][0]["verification"] is None


def test_same_key_same_run_and_no_second_message(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    first = ask(api, org, tid, owner, Q, key="same-key-12345")
    second = ask(api, org, tid, owner, "a different question text", key="same-key-12345")
    assert first.status == 202
    assert second.status == 200
    assert first.body["run_id"] == second.body["run_id"]
    assert first.body["message_id"] == second.body["message_id"]
    assert len(messages(world.engine, tid)) == 1
    assert outbox_count(world.engine, uuid.UUID(first.body["run_id"])) == 1
    assert len(audit_rows(world.engine, "chat.run.created", first.body["run_id"])) == 1


def test_same_key_is_per_thread_and_never_leaks_across_users(world: World) -> None:
    api, org = world.api, world.org_id
    t1, t2 = new_thread(api, org, world.t.owner), new_thread(api, org, world.t.owner)
    t3 = new_thread(api, org, world.t.admin)
    a = ask(api, org, t1, world.t.owner, Q, key="shared-key-0001")
    b = ask(api, org, t2, world.t.owner, Q, key="shared-key-0001")
    c = ask(api, org, t3, world.t.admin, Q, key="shared-key-0001")
    assert len({a.body["run_id"], b.body["run_id"], c.body["run_id"]}) == 3
    # the admin cannot reuse the owner's thread with the owner's key to learn about the run
    d = ask(api, org, t1, world.t.admin, Q, key="shared-key-0001")
    assert d.status == 404


def test_concurrent_same_key_creates_one_run(
    world: World, live_url: str, settings_override: Any
) -> None:
    settings_override(CHAT_MAX_CONCURRENT_RUNS=50, CHAT_MAX_RUNS_PER_MIN=1000)
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    url = f"{live_url}{base_path(org)}/threads/{tid}/runs"
    body = {"content": Q, "idempotency_key": "concurrent-key-1"}
    barrier = threading.Barrier(8)

    def post() -> httpx.Response:
        barrier.wait()
        return httpx.post(
            url, json=body, headers={"Authorization": f"Bearer {owner.token}"}, timeout=30
        )

    with ThreadPoolExecutor(8) as pool:
        results = [f.result() for f in [pool.submit(post) for _ in range(8)]]
    assert {r.status_code for r in results} <= {200, 202}, [r.text for r in results]
    run_ids = {r.json()["run_id"] for r in results}
    assert len(run_ids) == 1
    assert sum(r.status_code == 202 for r in results) == 1
    assert outbox_count(world.engine, uuid.UUID(run_ids.pop())) == 1
    assert len(messages(world.engine, tid)) == 1


def test_validation(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    url = f"{base_path(org)}/threads/{tid}/runs"
    good = {"content": "hello", "idempotency_key": "k" * 8}
    assert api.post(url, owner, {**good, "content": ""}).status == 422
    assert api.post(url, owner, {**good, "content": "   "}).status == 422
    assert api.post(url, owner, {**good, "content": "x" * 4001}).status == 422
    assert api.post(url, owner, {**good, "idempotency_key": "short"}).status == 422
    assert api.post(url, owner, {**good, "idempotency_key": "k" * 129}).status == 422
    assert api.post(url, owner, {**good, "surprise": 1}).status == 422
    assert api.post(url, owner, {**good, "workspace_ids": ["not-a-uuid"]}).status == 422
    assert api.post(url, owner, {"content": "hi"}).status == 422
    assert api.post(url, owner, {**good, "content": "x" * 4000}).status == 202


def test_requested_workspaces_only_narrow(world: World) -> None:
    api, org = world.api, world.org_id
    viewer = world.viewer
    tid = new_thread(api, org, viewer)
    foreign = uuid.uuid4()
    res = ask(
        api, org, tid, viewer, Q, workspace_ids=[str(world.ws), str(world.foreign_ws), str(foreign)]
    )
    assert res.status == 202
    snap = run_row(world.engine, uuid.UUID(res.body["run_id"]))["scope_snapshot"]
    assert snap["workspace_ids"] == [str(world.ws)]  # forged / foreign ids are dropped
    # an org member without workspace roles has an empty scope: still accepted, answers abstain
    other = new_thread(api, org, world.plain)
    res2 = ask(api, org, other, world.plain, Q)
    assert res2.status == 202
    assert (
        run_row(world.engine, uuid.UUID(res2.body["run_id"]))["scope_snapshot"]["workspace_ids"]
        == []
    )
    # owner asking for a subset only
    t3 = new_thread(api, org, world.t.owner)
    res3 = ask(api, org, t3, world.t.owner, Q, workspace_ids=[])
    assert (
        run_row(world.engine, uuid.UUID(res3.body["run_id"]))["scope_snapshot"]["workspace_ids"]
        == []
    )


def test_one_active_run_per_thread(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    first = ask(api, org, tid, owner, Q, key="first-key-0001")
    assert first.status == 202
    busy = ask(api, org, tid, owner, "again", key="second-key-001")
    assert busy.status == 409
    assert busy.body["detail"] == "run_in_progress"
    # the original key still replays while the run is active
    assert ask(api, org, tid, owner, Q, key="first-key-0001").status == 200
    api.post(f"{base_path(org)}/runs/{first.body['run_id']}/cancel", owner, {})
    assert ask(api, org, tid, owner, "again", key="second-key-001").status == 202


def test_concurrent_run_cap_and_rate_limit(world: World, settings_override: Any) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    settings_override(CHAT_MAX_CONCURRENT_RUNS=2, CHAT_MAX_RUNS_PER_MIN=100)
    threads = [new_thread(api, org, owner) for _ in range(3)]
    assert ask(api, org, threads[0], owner, Q).status == 202
    assert ask(api, org, threads[1], owner, Q).status == 202
    capped = ask(api, org, threads[2], owner, Q)
    assert capped.status == 409
    assert capped.body["detail"] == "too_many_runs"

    admin = world.t.admin
    settings_override(CHAT_MAX_CONCURRENT_RUNS=100, CHAT_MAX_RUNS_PER_MIN=2)
    admin_threads = [new_thread(api, org, admin) for _ in range(3)]
    assert ask(api, org, admin_threads[0], admin, Q).status == 202
    assert ask(api, org, admin_threads[1], admin, Q).status == 202
    limited = ask(api, org, admin_threads[2], admin, Q)
    assert limited.status == 429
    assert limited.body["detail"] == "rate_limited"
    assert int(limited.raw.headers["Retry-After"]) >= 1
    # a replay of an accepted run is not rate limited
    again = ask(api, org, admin_threads[0], admin, Q, key=run_key(world, admin_threads[0]))
    assert again.status == 200


def run_key(world: World, thread: uuid.UUID) -> str:
    with world.engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT idempotency_key FROM chat_runs WHERE thread_id = :t"), {"t": thread}
            ).scalar_one()
        )


# ----------------------------------------------------------------------------- run, cancel


def test_get_run_and_cancel_idempotent(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    run = ask_ok(api, org, tid, owner, Q)
    got = api.get(f"{base_path(org)}/runs/{run}", owner)
    assert got.status == 200
    assert set(got.body) == {
        "run_id",
        "thread_id",
        "state",
        "error_code",
        "message",
        "created_at",
        "finished_at",
    }
    assert got.body["state"] == "queued"
    assert got.body["message"] is None
    c1 = api.post(f"{base_path(org)}/runs/{run}/cancel", owner, {})
    c2 = api.post(f"{base_path(org)}/runs/{run}/cancel", owner, {})
    assert c1.status == 202
    assert c1.body == {"state": "cancelled"}
    assert c2.status == 202
    assert c2.body == {"state": "cancelled"}
    assert event_types(world.engine, run) == ["cancelled"]  # exactly once
    after = api.get(f"{base_path(org)}/runs/{run}", owner)
    assert after.body["state"] == "cancelled"
    assert after.body["finished_at"] is not None
    assert api.get(f"{base_path(org)}/threads/{tid}", owner).body["active_run"] is None


def test_deleting_a_thread_cancels_its_active_run(world: World) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    run = ask_ok(api, org, tid, owner, Q)
    assert api.delete(f"{base_path(org)}/threads/{tid}", owner).status == 204
    assert run_row(world.engine, run)["state"] == "cancelled"


# ------------------------------------------------------------------------------------ SSE


def test_sse_protocol_of_a_finished_run(world: World, live_url: str) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    run = ask_ok(api, org, tid, owner, Q)
    api.post(f"{base_path(org)}/runs/{run}/cancel", owner, {})
    status, headers, frames = read_sse(live_url, org, run, owner)
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-store"
    assert headers["x-accel-buffering"] == "no"
    assert frames[0].retry == 3000
    events = data_frames(frames)
    seq = run_events(world.engine, run)[0]["seq"]  # seq is a global identity: ordered, not 1..n
    assert [(f.id, f.event) for f in events] == [(seq, "cancelled")]
    assert events[0].data == {"reason": "user"}
    assert end_reason(frames) == "terminal"
    # Last-Event-ID (header or query) skips what was seen; a bad value reads as "from the start"
    _, _, resumed = read_sse(live_url, org, run, owner, last_event_id=seq)
    assert data_frames(resumed) == []
    assert end_reason(resumed) == "terminal"
    _, _, queried = read_sse(live_url, org, run, owner, last_event_id=seq, query_last_id=True)
    assert data_frames(queried) == []
    before = read_sse(live_url, org, run, owner, last_event_id=seq - 1)[2]
    assert [f.id for f in data_frames(before)] == [seq]
    assert [f.id for f in data_frames(read_sse(live_url, org, run, owner, last_event_id=0)[2])] == [
        seq
    ]


def test_sse_failed_event_from_the_reaper_gets_a_safe_message(world: World, live_url: str) -> None:
    api, org, owner = world.api, world.org_id, world.t.owner
    tid = new_thread(api, org, owner)
    run = ask_ok(api, org, tid, owner, Q)
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE chat_runs SET state = 'failed', error_code = 'lease_expired',"
                " finished_at = now() WHERE id = :r"
            ),
            {"r": run},
        )
        conn.execute(
            text(
                "INSERT INTO chat_run_events (run_id, org_id, type, data) VALUES"
                " (:r, :o, 'failed', '{\"error_code\": \"lease_expired\"}'::jsonb)"
            ),
            {"r": run, "o": org},
        )
    _, _, frames = read_sse(live_url, org, run, owner)
    failed = data_frames(frames)[-1]
    assert failed.event == "failed"
    assert failed.data["error_code"] == "lease_expired"
    assert failed.data["message"]


def test_sse_ends_unavailable_when_membership_is_removed(world: World, live_url: str) -> None:
    """Authorization is re-checked on every poll; a run that is still going gets no further data."""
    api, org = world.api, world.org_id
    member = world.other_plain
    tid = new_thread(api, org, member)
    run = ask_ok(api, org, tid, member, Q)  # stays queued: nothing executes it
    result: dict[str, Any] = {}

    def reader() -> None:
        result["frames"] = read_sse(live_url, org, run, member, timeout=60)[2]

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(1.5)  # connected and polling
    removed = api.delete(f"/v1/orgs/{org}/members/{member.id}", world.t.owner)
    assert removed.status == 204, removed.body
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert end_reason(result["frames"]) == "unavailable"
    assert data_frames(result["frames"]) == []


# ------------------------------------------------------------------------------- OpenAPI


def test_openapi_lists_every_chat_route(app: FastAPI, client: Any) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    base = "/v1/orgs/{org_id}/chat"
    expected = {
        (f"{base}/threads", "post"),
        (f"{base}/threads", "get"),
        (f"{base}/threads/{{thread_id}}", "get"),
        (f"{base}/threads/{{thread_id}}", "patch"),
        (f"{base}/threads/{{thread_id}}", "delete"),
        (f"{base}/threads/{{thread_id}}/runs", "post"),
        (f"{base}/runs/{{run_id}}", "get"),
        (f"{base}/runs/{{run_id}}/cancel", "post"),
        (f"{base}/runs/{{run_id}}/events", "get"),
    }
    for path, method in expected:
        assert method in paths.get(path, {}), (path, method)
    assert "text/event-stream" in json.dumps(paths[f"{base}/runs/{{run_id}}/events"]["get"])
