"""Sync (M06.01.05), idempotency, failure handling and revocation (M06.01.06) on a fake GitHub."""

from __future__ import annotations

import uuid
from typing import Any

from catalog_support import World
from fake_github import FakeGitHub
from m06_support import TOKEN, create_source, docs_of, request_sync, rows, run, scalar
from sqlalchemy import text

from app.modules.connectors.port import FetchedItem, RemoteItem
from app.modules.connectors.sync import SyncDeps


def _seed(gh: FakeGitHub) -> None:
    gh.commit(
        {
            "README.md": b"# Hello\nA readme about herons.",
            "docs/a.md": b"alpha document",
            "src/x.py": b"print('x')\n",
            "logo.png": b"\x89PNG\r\n\x00binary",  # not an allowed type: never synced
            "node_modules/pkg/i.js": b"skip me",  # vendor dir: never synced
        }
    )


def _cursor(admin: Any, source_id: str) -> str | None:
    return scalar(admin, "SELECT sync_cursor FROM sources WHERE id = :s", s=uuid.UUID(source_id))


def test_first_listing_then_update_delete_and_replay_are_idempotent(
    world: World, deps: SyncDeps, fake_gh: FakeGitHub
) -> None:
    admin = world.engine
    _seed(fake_gh)
    source = create_source(world)
    sid = source["id"]
    assert TOKEN not in str(source)  # the token is never echoed

    assert run(deps, world, sid) == "succeeded"
    docs = docs_of(admin, sid)
    assert set(docs) == {"README.md", "docs/a.md", "src/x.py"}
    assert all(d.versions == 1 and not d.deleted for d in docs.values())
    assert _cursor(admin, sid) == fake_gh.head
    jobs = scalar(admin, "SELECT count(*) FROM ingestion_jobs WHERE org_id = :o", o=world.org_id)
    assert jobs == 3  # every synced file enters the normal ingestion path
    first_cursor = fake_gh.head

    # Replaying the unchanged repository creates nothing.
    assert request_sync(world, sid).status == 202
    assert run(deps, world, sid) == "succeeded"
    assert {t: d.versions for t, d in docs_of(admin, sid).items()} == dict.fromkeys(docs, 1)

    # An edit, a new file and a removal.
    fake_gh.commit(
        {"docs/a.md": b"alpha document, revised", "docs/b.md": b"beta", "src/x.py": None}
    )
    assert request_sync(world, sid).status == 202
    assert run(deps, world, sid) == "succeeded"
    docs = docs_of(admin, sid)
    assert docs["docs/a.md"].versions == 2  # same document, new version
    assert docs["docs/b.md"].versions == 1
    assert docs["src/x.py"].deleted is True  # logical deletion
    assert docs["README.md"].versions == 1
    purge = scalar(
        admin,
        "SELECT count(*) FROM outbox_events WHERE event_type = 'document.deleted'"
        " AND aggregate_id = :d",
        d=docs["src/x.py"].id,
    )
    assert purge == 1
    last = rows(
        admin,
        "SELECT stats FROM source_syncs WHERE source_id = :s ORDER BY created_at DESC LIMIT 1",
        s=uuid.UUID(sid),
    )[0].stats
    assert (last["new"], last["updated"], last["deleted"]) == (1, 1, 1)

    # Replay of an OLD cursor (same delta again): no duplicates, the deleted file stays deleted.
    with admin.begin() as conn:
        conn.execute(
            text("UPDATE sources SET sync_cursor = :c WHERE id = :s"),
            {"c": first_cursor, "s": uuid.UUID(sid)},
        )
    assert request_sync(world, sid).status == 202
    assert run(deps, world, sid) == "succeeded"
    again = docs_of(admin, sid)
    assert {t: d.versions for t, d in again.items()} == {t: d.versions for t, d in docs.items()}
    assert len(again) == len(docs)

    # The token authenticated every upstream call and is nowhere in the database.
    assert set(fake_gh.auth_seen) == {f"Bearer {TOKEN}"}
    hits = 0
    for table in ("connector_accounts", "sources", "source_syncs", "source_items", "audit_events"):
        hits += scalar(
            admin, f"SELECT count(*) FROM {table} t WHERE t::text LIKE :p", p=f"%{TOKEN}%"
        )
    assert hits == 0
    blob = scalar(
        admin, "SELECT token_ciphertext FROM connector_accounts WHERE org_id = :o", o=world.org_id
    )
    assert blob is not None and TOKEN.encode() not in bytes(blob)


def test_a_failed_sync_keeps_the_cursor_and_the_next_one_recovers(
    world: World, deps: SyncDeps, fake_gh: FakeGitHub
) -> None:
    admin = world.engine
    _seed(fake_gh)
    sid = create_source(world, token=None)["id"]
    assert run(deps, world, sid) == "succeeded"
    cursor = _cursor(admin, sid)

    fake_gh.commit({"docs/new.md": b"brand new"})
    fake_gh.fail_status = 500
    assert request_sync(world, sid).status == 202
    assert run(deps, world, sid) == "failed"
    assert _cursor(admin, sid) == cursor  # not advanced
    assert "docs/new.md" not in docs_of(admin, sid)
    failed = rows(
        admin,
        "SELECT state, error_code FROM source_syncs WHERE source_id = :s"
        " ORDER BY created_at DESC LIMIT 1",
        s=uuid.UUID(sid),
    )[0]
    assert (failed.state, failed.error_code) == ("failed", "upstream_error")

    fake_gh.fail_status = None
    assert request_sync(world, sid).status == 202
    assert run(deps, world, sid) == "succeeded"
    assert "docs/new.md" in docs_of(admin, sid) and _cursor(admin, sid) == fake_gh.head

    # Rate limit: stop, record, push the next run out; nothing is hammered.
    fake_gh.commit({"docs/more.md": b"more"})
    fake_gh.rate_limited = True
    assert request_sync(world, sid).status == 202
    assert run(deps, world, sid) == "failed"
    wait = scalar(
        admin,
        "SELECT extract(epoch FROM next_sync_at - now()) FROM sources WHERE id = :s",
        s=uuid.UUID(sid),
    )
    assert wait is not None and wait > 30
    assert (
        scalar(
            admin,
            "SELECT error_code FROM source_syncs WHERE source_id = :s"
            " ORDER BY created_at DESC LIMIT 1",
            s=uuid.UUID(sid),
        )
        == "rate_limited"
    )


def test_revoke_blocks_everything_at_once(
    world: World, deps: SyncDeps, fake_gh: FakeGitHub
) -> None:
    admin, api = world.engine, world.api
    _seed(fake_gh)
    sid = create_source(world)["id"]
    assert run(deps, world, sid) == "succeeded"
    docs = docs_of(admin, sid)
    doc_id = docs["README.md"].id
    assert api.get(world.doc_path(doc_id), world.t.owner).status == 200
    assert request_sync(world, sid).status == 202  # queued, not yet run

    res = api.post(world.path(f"/sources/{sid}/revoke"), world.ws_admin, {})
    assert res.status == 200 and res.body["state"] == "revoked"
    # Same transaction: documents 404 for everyone, token wiped, queued sync cancelled.
    assert api.get(world.doc_path(doc_id), world.t.owner).status == 404
    assert all(d.deleted for d in docs_of(admin, sid).values())
    assert (
        scalar(
            admin,
            "SELECT token_ciphertext FROM connector_accounts WHERE org_id = :o",
            o=world.org_id,
        )
        is None
    )
    states = {
        r.state
        for r in rows(
            admin, "SELECT state FROM source_syncs WHERE source_id = :s", s=uuid.UUID(sid)
        )
    }
    assert "queued" not in states and "cancelled" in states
    assert run(deps, world, sid) == "skipped"  # nothing runs for a revoked source
    assert request_sync(world, sid).status == 409
    assert (
        api.post(world.path(f"/sources/{sid}/revoke"), world.ws_admin, {}).status == 200
    )  # idempotent


def test_revoke_during_a_running_sync_stops_further_writes(
    world: World, deps: SyncDeps, fake_gh: FakeGitHub
) -> None:
    admin = world.engine
    fake_gh.commit({f"docs/f{i}.md": f"file {i}".encode() for i in range(4)})
    sid = create_source(world, token=None)["id"]
    inner_factory = deps.connector_factory
    fetched: list[str] = []

    def factory(config: dict[str, Any], token: str | None) -> Any:
        inner = inner_factory(config, token)

        class Revoking:
            def validate(self, cfg: Any) -> Any:
                return inner.validate(cfg)

            def discover(self, cursor: str | None) -> Any:
                return inner.discover(cursor)

            def fetch(self, item: RemoteItem) -> FetchedItem:
                result = inner.fetch(item)
                fetched.append(item.path)
                if len(fetched) == 2:  # the admin revokes while the sync is mid-run
                    res = world.api.post(world.path(f"/sources/{sid}/revoke"), world.ws_admin, {})
                    assert res.status == 200
                return result

        return Revoking()

    deps.connector_factory = factory
    assert run(deps, world, sid) == "revoked"
    docs = docs_of(admin, sid)
    assert len(docs) <= 2 and all(d.deleted for d in docs.values())  # nothing live survives
    assert len(fetched) <= 3
