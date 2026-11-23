"""M13.02 capture + M13.03 feedback through the real app on PostgreSQL + RLS."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from catalog_support import World
from m03_support import make_url
from sqlalchemy import Engine, create_engine, text

from app.core.db import tenant_session
from app.modules.knowledge.deletion import deny_in_postgres
from app.modules.observability import capture
from app.workers.worker_db import worker_session


@pytest.fixture(scope="session")
def worker_engine(stack, migrated) -> Iterator[Engine]:  # type: ignore[no-untyped-def]
    engine = create_engine(make_url(stack, "worker"), pool_pre_ping=True)
    yield engine
    engine.dispose()


def _cap_path(world: World, sub: str = "") -> str:
    return f"/v1/orgs/{world.org_id}/ai-captures{sub}"


def _seed_run(world: World) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A thread owned by ``t.member`` with one answered run: (thread, run, assistant message)."""
    thread, run, user_msg, answer = (uuid.uuid4() for _ in range(4))
    org, member = world.org_id, world.t.member.id
    chunk = uuid.uuid4()
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chat_threads (id, org_id, workspace_id, created_by)"
                " VALUES (:t, :o, :w, :u)"
            ),
            {"t": thread, "o": org, "w": world.ws, "u": member},
        )
        conn.execute(
            text(
                "INSERT INTO chat_messages (id, org_id, thread_id, role, content)"
                " VALUES (:m, :o, :t, 'user', 'q')"
            ),
            {"m": user_msg, "o": org, "t": thread},
        )
        conn.execute(
            text(
                "INSERT INTO chat_runs (id, org_id, thread_id, user_message_id, created_by,"
                " state, idempotency_key, finished_at)"
                " VALUES (:r, :o, :t, :m, :u, 'completed', :k, now())"
            ),
            {"r": run, "o": org, "t": thread, "m": user_msg, "u": member, "k": uuid.uuid4().hex},
        )
        conn.execute(
            text(
                "INSERT INTO chat_messages (id, org_id, thread_id, role, content, citations,"
                " run_id) VALUES (:m, :o, :t, 'assistant', 'a',"
                " CAST(:c AS jsonb), :r)"
            ),
            {
                "m": answer,
                "o": org,
                "t": thread,
                "r": run,
                "c": f'[{{"chunk_id": "{chunk}"}}]',
            },
        )
    return thread, run, answer


def _record(world: World, worker_engine: Engine, run: uuid.UUID, docs: list[uuid.UUID]) -> bool:
    with worker_session(worker_engine, world.org_id) as session:
        return capture.record_capture(
            session,
            org_id=world.org_id,
            run_id=run,
            workspace_id=world.ws,
            prompt_version_id=None,
            messages=[("user", "mail me at jane.doe@example.com card 4111 1111 1111 1111")],
            answer="Call 0123456789012 sk-abcdefghijklmnopqrstuvwxyz",
            chunk_ids=[uuid.uuid4()],
            document_ids=docs,
        )


def _count(world: World, run: uuid.UUID | None = None) -> int:
    with world.engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM ai_run_captures WHERE org_id = :o"),
                {"o": world.org_id},
            ).scalar_one()
        )


def test_capture_off_by_default_then_redacted_admin_only(
    world: World, worker_engine: Engine
) -> None:
    api = world.api
    _, run, _ = _seed_run(world)
    assert _record(world, worker_engine, run, []) is False  # default: off -> nothing written
    assert _count(world) == 0
    assert api.get(_cap_path(world, "/settings"), world.t.owner).body["capture_mode"] == "off"

    assert (
        api.put(_cap_path(world, "/settings"), world.t.owner, {"capture_mode": "x"}).status == 422
    )
    over = {"capture_mode": "redacted", "capture_retention_days": 91}
    assert api.put(_cap_path(world, "/settings"), world.t.owner, over).status == 422
    ok = api.put(
        _cap_path(world, "/settings"),
        world.t.owner,
        {"capture_mode": "redacted", "capture_retention_days": 30},
    )
    assert ok.status == 200
    assert ok.body["capture_retention_days"] == 30

    assert _record(world, worker_engine, run, []) is True
    listed = api.get(_cap_path(world), world.t.admin)
    assert listed.status == 200
    assert len(listed.body["items"]) == 1
    detail = api.get(_cap_path(world, f"/{listed.body['items'][0]['id']}"), world.t.owner)
    blob = detail.body["messages"] + detail.body["answer"]
    assert "jane.doe@example.com" not in blob
    assert "4111" not in blob
    assert "0123456789012" not in blob
    assert "sk-abcdef" not in blob

    cap_id = listed.body["items"][0]["id"]
    assert api.get(_cap_path(world), world.t.member).status == 403  # plain member
    assert api.get(_cap_path(world, f"/{cap_id}"), world.foreign.owner).status == 404  # outsider
    assert api.delete(_cap_path(world, f"/{cap_id}"), world.foreign.owner).status == 404
    assert api.delete(_cap_path(world, f"/{cap_id}"), world.t.owner).status == 204
    assert api.get(_cap_path(world, f"/{cap_id}"), world.t.owner).status == 404


def test_full_admin_only_mode_keeps_text(
    world: World, worker_engine: Engine, app_engine: Engine
) -> None:
    _, run, _ = _seed_run(world)
    with tenant_session(
        user_id=world.t.owner.id, org_id=world.org_id, engine=app_engine
    ) as session:
        capture.save_settings(
            session,
            world.org_id,
            mode="full_admin_only",
            retention_days=None,
            updated_by=world.t.owner.id,
        )
    assert _record(world, worker_engine, run, [])
    with world.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT messages FROM ai_run_captures WHERE org_id = :o"), {"o": world.org_id}
        ).scalar_one()
    assert "jane.doe@example.com" in stored


def test_retention_sweep_and_document_deletion(world: World, worker_engine: Engine) -> None:
    capture_mode = {"capture_mode": "redacted", "capture_retention_days": 14}
    assert world.api.put(_cap_path(world, "/settings"), world.t.owner, capture_mode).status == 200
    _, run1, _ = _seed_run(world)
    _, run2, _ = _seed_run(world)
    _, run3, _ = _seed_run(world)
    doc = world.seed(title="Gone", deleted=True).id
    keep_doc = uuid.uuid4()
    assert _record(world, worker_engine, run1, [])  # will expire
    assert _record(world, worker_engine, run2, [doc])  # deleted with its document
    assert _record(world, worker_engine, run3, [keep_doc])  # untouched
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ai_run_captures SET expires_at = now() - interval '1 second'"
                " WHERE run_id = :r"
            ),
            {"r": run1},
        )
    assert capture.sweep_expired(worker_engine) >= 1
    assert _count(world) == 2

    with worker_session(worker_engine, world.org_id) as session:
        deny_in_postgres(session, world.org_id, doc)
    with world.engine.connect() as conn:
        left = (
            conn.execute(
                text("SELECT run_id FROM ai_run_captures WHERE org_id = :o"), {"o": world.org_id}
            )
            .scalars()
            .all()
        )
    assert left == [run3]


def _fb(world: World, message: uuid.UUID, sub: str = "") -> str:
    return f"/v1/orgs/{world.org_id}/chat/messages/{message}/feedback{sub}"


def test_feedback_validation_replace_isolation_and_queue(world: World, app_engine: Engine) -> None:
    api = world.api
    _, run, message = _seed_run(world)
    member = world.t.member
    assert api.get(_fb(world, message), member).body is None
    assert api.put(_fb(world, message), member, {"rating": "up", "reason": "other"}).status == 422
    assert api.put(_fb(world, message), member, {"rating": "down", "reason": "bad"}).status == 422
    assert api.put(_fb(world, message), member, {"rating": "up", "note": "x"}).status == 422
    assert api.put(_fb(world, message), member, {"rating": "up"}).status == 200
    down = api.put(_fb(world, message), member, {"rating": "down", "reason": "wrong_citation"})
    assert down.status == 200
    assert down.body["rating"] == "down"
    with world.engine.connect() as conn:  # unique per (message, user): the vote was replaced
        n = conn.execute(
            text("SELECT count(*) FROM answer_feedback WHERE message_id = :m"), {"m": message}
        ).scalar_one()
    assert n == 1

    # another member of the org and an outsider cannot see the message at all
    for other in (world.other_plain, world.foreign.owner):
        assert api.get(_fb(world, message), other).status == 404
        assert api.put(_fb(world, message), other, {"rating": "down"}).status == 404
    # RLS: another member's session reads no votes; an org admin reads them
    with tenant_session(user_id=world.other_plain.id, org_id=world.org_id, engine=app_engine) as s:
        assert s.execute(text("SELECT count(*) FROM answer_feedback")).scalar_one() == 0
    with tenant_session(user_id=world.t.owner.id, org_id=world.org_id, engine=app_engine) as s:
        assert s.execute(text("SELECT count(*) FROM answer_feedback")).scalar_one() == 1

    queue_path = f"/v1/orgs/{world.org_id}/feedback/review-queue"
    assert api.get(queue_path, member).status == 403
    assert api.get(queue_path, world.foreign.owner).status == 404
    queue = api.get(queue_path, world.t.owner)
    assert queue.status == 200
    assert len(queue.body["items"]) == 1
    item = queue.body["items"][0]
    assert item["run_id"] == str(run)
    assert item["reason"] == "wrong_citation"
    assert len(item["chunk_ids"]) == 1
