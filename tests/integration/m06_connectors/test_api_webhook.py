"""Sources API authorization, webhook ingestion and the scheduler (M06.01.01/.04)."""

from __future__ import annotations

import hashlib
import hmac
import uuid

from catalog_support import World
from fake_github import FakeGitHub
from m06_support import TOKEN, create_source, request_sync, rows, run, scalar
from sqlalchemy import text

from app.modules.connectors.models import EVENT_SOURCE_SYNC_REQUESTED
from app.modules.connectors.sync import SyncDeps
from app.workers import tasks
from app.workers.celery_app import TASK_CONNECTOR_SYNC
from app.workers.dispatcher import ROUTES, OutboxRow


def test_routes_are_admin_only_and_tenant_scoped(world: World) -> None:
    api = world.api
    body = {"provider": "github", "repo": "acme/docs"}
    assert api.post(world.path("/sources"), world.editor, body).status == 403  # editor: no
    assert api.post(world.path("/sources"), world.t.outsider, body).status == 404  # not a member
    assert api.post(world.path("/sources"), world.ws_admin, {**body, "repo": "bad"}).status == 422

    created = create_source(world)
    sid = created["id"]
    assert created["has_token"] is True and created["webhook_secret"]
    assert TOKEN not in str(created)

    got = api.get(world.path(f"/sources/{sid}"), world.ws_admin)
    assert got.status == 200 and TOKEN not in str(got.body) and "webhook_secret" not in got.body
    assert got.body["last_sync"]["state"] == "queued"  # creation queued the first sync
    assert [s["id"] for s in api.get(world.path("/sources"), world.ws_admin).body] == [sid]
    assert api.get(world.path(f"/sources/{sid}"), world.editor).status == 403

    # Cross-org: another org's owner cannot reach it by any path.
    foreign = world.foreign.owner
    assert api.get(world.path(f"/sources/{sid}"), foreign).status == 404
    own = f"/v1/orgs/{world.foreign.org_id}/workspaces/{world.foreign_ws}/sources/{sid}"
    assert api.get(own, foreign).status == 404
    assert api.post(own + "/revoke", foreign, {}).status == 404

    # Audit trail has ids only, never the token.
    assert (
        scalar(
            world.engine, "SELECT count(*) FROM audit_events WHERE event_type = 'source.created'"
        )
        >= 1
    )
    leaked = scalar(
        world.engine, "SELECT count(*) FROM audit_events a WHERE a::text LIKE :p", p=f"%{TOKEN}%"
    )
    assert leaked == 0


def _sign(secret: str, body: bytes) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "X-GitHub-Event": "push"}


def test_webhook_signature_duplicate_and_revoked(
    world: World, deps: SyncDeps, fake_gh: FakeGitHub
) -> None:
    client = world.api.client
    fake_gh.commit({"README.md": b"hello"})
    created = create_source(world, token=None)
    sid, secret = created["id"], created["webhook_secret"]
    assert run(deps, world, sid) == "succeeded"  # consume the creation sync
    url = f"/v1/webhooks/github/{sid}"
    body = b'{"ref":"refs/heads/main"}'

    bad = client.post(url, content=body, headers=_sign("wrong-secret", body))
    assert bad.status_code == 401 and bad.json() == {"detail": "unauthorized"}
    assert client.post(url, content=body).status_code == 401  # no signature at all
    unknown = f"/v1/webhooks/github/{uuid.uuid4()}"
    assert client.post(unknown, content=body, headers=_sign(secret, body)).status_code == 401

    headers = {**_sign(secret, body), "X-GitHub-Delivery": "delivery-1"}
    ok = client.post(url, content=body, headers=headers)
    assert ok.status_code == 202 and ok.json() == {"status": "queued"}
    queued = lambda: scalar(  # noqa: E731
        world.engine,
        "SELECT count(*) FROM source_syncs WHERE source_id = :s AND state = 'queued'",
        s=uuid.UUID(sid),
    )
    assert queued() == 1
    again = client.post(url, content=body, headers=headers)  # the same delivery, redelivered
    assert again.status_code == 202 and again.json() == {"status": "duplicate"}
    assert queued() == 1
    assert run(deps, world, sid) == "succeeded"
    still = client.post(url, content=body, headers=headers)  # replay after the sync ran
    assert still.json() == {"status": "duplicate"} and queued() == 0
    ping = {**_sign(secret, body), "X-GitHub-Event": "ping"}
    assert client.post(url, content=body, headers=ping).json() == {"status": "ignored"}

    # A revoked source answers like an unknown one and its secret is gone.
    assert world.api.post(world.path(f"/sources/{sid}/revoke"), world.ws_admin, {}).status == 200
    after = client.post(url, content=body, headers={**headers, "X-GitHub-Delivery": "d2"})
    assert after.status_code == 401


def test_scheduler_queues_due_sources_once_and_routes_to_the_sync_task(
    world: World, deps: SyncDeps, fake_gh: FakeGitHub
) -> None:
    admin = world.engine
    fake_gh.commit({"README.md": b"hello"})
    sid = create_source(world, token=None)["id"]
    assert run(deps, world, sid) == "succeeded"
    with admin.begin() as conn:
        conn.execute(
            text("UPDATE sources SET next_sync_at = now() - interval '1 minute' WHERE id = :s"),
            {"s": uuid.UUID(sid)},
        )
    with deps.engine.begin() as conn:  # the dispatcher's worker role
        assert conn.execute(text("SELECT connector_enqueue_due_syncs(10)")).scalar_one() >= 1
        assert conn.execute(text("SELECT connector_enqueue_due_syncs(10)")).scalar_one() == 0
    events = rows(
        admin,
        "SELECT id, seq, org_id, aggregate_type, aggregate_id, event_type, payload, attempts"
        " FROM outbox_events WHERE event_type = :t AND aggregate_id = :s ORDER BY seq DESC",
        t=EVENT_SOURCE_SYNC_REQUESTED,
        s=uuid.UUID(sid),
    )
    assert len(events) == 2  # creation + this schedule tick
    delivery = ROUTES[EVENT_SOURCE_SYNC_REQUESTED](OutboxRow(*events[0]))
    assert (delivery.task, delivery.queue) == (TASK_CONNECTOR_SYNC, "maintenance")
    assert delivery.args == (str(world.org_id), sid)

    # The Celery task body runs the queued (scheduled) sync through the injected deps.
    tasks.set_connector_deps_override(deps)
    try:
        assert tasks.sync_source(*delivery.args) == "succeeded"
        assert tasks.sync_source("not-a-uuid", sid) == "invalid"
    finally:
        tasks.set_connector_deps_override(None)
    assert request_sync(world, sid).status == 202
