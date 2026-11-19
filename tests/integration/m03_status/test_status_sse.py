"""M03.03.04: the SSE status streams (replay, terminal close, re-authorization, no held sessions).

The Starlette ``TestClient`` returns once the response has finished, so every stream here is
bounded (a terminal state, a short ``MAX_SECONDS`` or an authorization loss ends it) and the
"live" changes are made by a timer thread while the request is open.
"""

from __future__ import annotations

import uuid

import pytest
from catalog_support import World
from sqlalchemy import text
from status_support import (
    FORBIDDEN_FRAGMENTS,
    Frame,
    add_event,
    after_delay,
    read_stream,
    seed_job,
    set_job,
)
from tenancy_support import assert_denied

from app.modules.ingestion import events_stream


@pytest.fixture(autouse=True)
def fast_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_stream, "POLL_SECONDS", 0.1)
    monkeypatch.setattr(events_stream, "HEARTBEAT_SECONDS", 0.25)
    monkeypatch.setattr(events_stream, "MAX_SECONDS", 4.0)


def doc_events(world: World, doc_id: uuid.UUID | str) -> str:
    return world.doc_path(doc_id, "/ingestion/events")


def kinds(frames: list[Frame]) -> list[str | None]:
    return [f.event for f in frames if f.comment is None]


def jobs(frames: list[Frame]) -> list[Frame]:
    return [f for f in frames if f.event == "job"]


def test_terminal_document_gets_snapshot_then_end(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="dead", error_code="too_large", events=(("queued", None, 0),))
    status, headers, raw, frames = read_stream(world, doc_events(world, doc.id), world.viewer)
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-store"
    assert headers["x-accel-buffering"] == "no"
    assert kinds(frames) == ["retry", "snapshot", "end"]
    snapshot = frames[1]
    assert snapshot.data["ingestion"]["state"] == "failed"
    assert snapshot.data["ingestion"]["error_code"] == "too_large"
    assert snapshot.id == str(snapshot.data["cursor"])  # fresh connect: id = the cursor
    assert frames[2].data == {"reason": "terminal"}
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in raw


def test_resume_replays_only_newer_events_in_order(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="dead", error_code="timeout", events=(("queued", None, 0),))
    ids = [
        add_event(world, job, "started"),
        add_event(world, job, "stage_started", "extract"),
        add_event(world, job, "stage_completed", "extract"),
        add_event(world, job, "dead"),
    ]
    _, _, raw, frames = read_stream(
        world, doc_events(world, doc.id), world.viewer, {"Last-Event-ID": str(ids[1])}
    )
    replayed = jobs(frames)
    assert [f.id for f in replayed] == [str(ids[2]), str(ids[3])]
    assert [f.data["type"] for f in replayed] == ["stage_completed", "dead"]
    assert replayed[0].data["stage"] == "extract"
    assert replayed[0].data["ingestion"]["state"] == "failed"
    assert frames[1].event == "snapshot"
    assert frames[1].id is None  # a resume keeps the client's Last-Event-ID
    assert frames[-1].event == "end"
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in raw


def test_resume_via_query_parameter_and_garbage_ids(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="dead", error_code="timeout")
    first = add_event(world, job, "started")
    second = add_event(world, job, "dead")
    path = doc_events(world, doc.id) + f"?last_event_id={first}"
    _, _, _, frames = read_stream(world, path, world.viewer)
    assert [f.id for f in jobs(frames)] == [str(second)]
    # A non-numeric Last-Event-ID is ignored: fresh connect, no replay.
    _, _, _, frames = read_stream(
        world, doc_events(world, doc.id), world.viewer, {"Last-Event-ID": "abc"}
    )
    assert jobs(frames) == []


def test_replay_beyond_the_bound_falls_back_to_a_snapshot(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="dead", error_code="timeout", events=())
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id,"
                " version_id, event_type, attempt) SELECT :j, :o, :w, :d, :v, 'stage_started', n"
                " FROM generate_series(1, 600) n"
            ),
            {"j": job.job_id, "o": world.org_id, "w": world.ws, "d": doc.id, "v": job.version_id},
        )
    _, _, _, frames = read_stream(
        world, doc_events(world, doc.id), world.viewer, {"Last-Event-ID": "0"}
    )
    assert jobs(frames) == []
    assert kinds(frames) == ["retry", "snapshot", "end"]
    assert frames[1].data["ingestion"]["state"] == "failed"
    # Within the bound everything is replayed.
    tail = int(frames[1].data["cursor"]) - 400
    _, _, _, frames = read_stream(
        world, doc_events(world, doc.id), world.viewer, {"Last-Event-ID": str(tail)}
    )
    assert len(jobs(frames)) == 400


def test_live_events_then_terminal_closes_the_stream(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="running", attempts=1, stage="extract", epoch=1)

    def progress() -> None:
        add_event(world, job, "stage_completed", "extract")
        add_event(world, job, "stage_started", "embed")
        set_job(world, job, state="succeeded", current_stage=None)
        add_event(world, job, "succeeded")

    timer = after_delay(0.5, progress)
    status, _, raw, frames = read_stream(world, doc_events(world, doc.id), world.viewer)
    timer.join()
    assert status == 200
    snapshot = frames[1]
    assert snapshot.data["ingestion"]["state"] == "processing"
    live = jobs(frames)
    # Only events created after the connect (the seeded "queued" is history).
    assert [f.data["type"] for f in live][:2] == ["stage_completed", "stage_started"]
    assert frames[-1].event == "end"
    assert frames[-1].data == {"reason": "terminal"}
    ids = [int(f.id or 0) for f in live]
    assert ids == sorted(ids)
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in raw


def test_idle_stream_sends_heartbeats_and_ends_by_timeout(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events_stream, "MAX_SECONDS", 1.2)
    doc = world.seed()
    seed_job(world, doc, state="running", attempts=1, epoch=1)
    _, _, raw, frames = read_stream(world, doc_events(world, doc.id), world.viewer)
    assert ": keep-alive" in raw
    assert frames[-1].event == "end"
    assert frames[-1].data == {"reason": "timeout"}


def test_stream_ends_when_permission_is_removed(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="running", attempts=1, epoch=1)

    def revoke() -> None:
        with world.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM workspace_memberships WHERE workspace_id = :w AND user_id = :u"),
                {"w": world.ws, "u": world.editor.id},
            )

    timer = after_delay(0.5, revoke)
    status, _, raw, frames = read_stream(world, doc_events(world, doc.id), world.editor)
    timer.join()
    assert status == 200
    assert frames[-1].event == "end"
    assert frames[-1].data == {"reason": "unavailable"}
    assert "forbidden" not in raw
    assert "not_workspace_member" not in raw


def test_stream_ends_when_the_document_is_deleted(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="running", attempts=1, epoch=1)
    timer = after_delay(0.5, lambda: world.api.delete(world.doc_path(doc.id), world.t.owner))
    _, _, _, frames = read_stream(world, doc_events(world, doc.id), world.viewer)
    timer.join()
    assert frames[-1].data == {"reason": "unavailable"}


def test_connect_authorization_and_concealment(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="dead", error_code="timeout")
    for user in (world.viewer, world.editor, world.t.owner, world.t.admin):
        status, *_ = read_stream(world, doc_events(world, doc.id), user)
        assert status == 200
    status, *_ = read_stream(world, doc_events(world, doc.id), None)
    assert status == 401
    for user, reason in (
        (world.plain, "not_workspace_member"),
        (world.t.outsider, "not_org_member"),
    ):
        res = world.api.get(doc_events(world, doc.id), user)
        assert_denied(world.api, res, 404, action="document.read", reason=reason)
    # Another org's user asking through the right path, and our user for a foreign/unknown id.
    assert world.api.get(doc_events(world, doc.id), world.foreign.owner).status == 404
    foreign = world.seed_foreign()
    assert world.api.get(doc_events(world, foreign.id), world.t.owner).status == 404
    assert world.api.get(doc_events(world, uuid.uuid4()), world.t.owner).status == 404


def test_stream_never_leaks_other_orgs_or_documents(world: World) -> None:
    mine = world.seed()
    other = world.seed()
    foreign = world.seed_foreign()
    my_job = seed_job(world, mine, state="dead", error_code="timeout")
    other_job = seed_job(world, other, state="dead", error_code="timeout")
    seed_job(world, foreign, org_id=world.foreign.org_id, workspace_id=world.foreign_ws)
    add_event(world, other_job, "started")
    mark = add_event(world, my_job, "started")
    _, _, raw, frames = read_stream(
        world, doc_events(world, mine.id), world.viewer, {"Last-Event-ID": "0"}
    )
    assert jobs(frames)[-1].id == str(mark)
    assert all(f.data["document_id"] == str(mine.id) for f in jobs(frames))
    assert str(other.id) not in raw
    assert str(foreign.id) not in raw


def test_workspace_stream(world: World) -> None:
    queued = world.seed()
    done = world.seed()
    q_job = seed_job(world, queued, state="queued")
    d_job = seed_job(world, done, state="running", attempts=1, epoch=1)
    foreign = world.seed_foreign()
    seed_job(world, foreign, org_id=world.foreign.org_id, workspace_id=world.foreign_ws)

    def progress() -> None:
        add_event(world, d_job, "stage_started", "chunk")

    timer = after_delay(0.4, progress)
    path = world.path("/ingestion/events")
    status, _, raw, frames = read_stream(world, path, world.viewer)
    timer.join()
    assert status == 200
    snapshot = frames[1]
    assert snapshot.event == "snapshot"
    active = {d["document_id"]: d["ingestion"]["state"] for d in snapshot.data["documents"]}
    assert active == {str(queued.id): "queued", str(done.id): "processing"}
    live = jobs(frames)
    assert [f.data["type"] for f in live] == ["stage_started"]
    assert live[0].data["document_id"] == str(done.id)
    assert frames[-1].data == {"reason": "timeout"}  # a workspace stream never goes terminal
    assert str(foreign.id) not in raw
    assert q_job.job_id


def test_workspace_stream_authorization(world: World) -> None:
    for user in (world.viewer, world.editor, world.t.owner, world.t.admin, world.ws_admin):
        status, *_ = read_stream(world, world.path("/ingestion/events"), user)
        assert status == 200, user
    assert read_stream(world, world.path("/ingestion/events"), None)[0] == 401
    res = world.api.get(world.path("/ingestion/events"), world.plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    res = world.api.get(world.path("/ingestion/events"), world.t.outsider)
    assert_denied(world.api, res, 404, action="document.read", reason="not_org_member")
    # A grant on one document shows that document, not the workspace feed.
    doc = world.seed()
    world.grant(doc.id, world.other_plain, "read")
    res = world.api.get(world.path("/ingestion/events"), world.other_plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    assert world.api.get(world.path("/ingestion/events"), world.foreign.owner).status == 404


def test_no_database_session_is_held_while_waiting(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream polls in short transactions: nothing is ``idle in transaction`` between polls,
    so a client that disappears leaves no session behind."""
    monkeypatch.setattr(events_stream, "POLL_SECONDS", 1.0)
    monkeypatch.setattr(events_stream, "MAX_SECONDS", 2.5)
    doc = world.seed()
    seed_job(world, doc, state="running", attempts=1, epoch=1)
    observed: list[int] = []

    def probe() -> None:
        row = world.row(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND usename = :u AND state LIKE 'idle in transaction%'",
            u="ukos_app",
        )
        observed.append(int(row[0]))

    timers = [after_delay(0.5, probe), after_delay(1.5, probe)]
    status, *_ = read_stream(world, doc_events(world, doc.id), world.viewer)
    for t in timers:
        t.join()
    assert status == 200
    assert observed == [0, 0]
