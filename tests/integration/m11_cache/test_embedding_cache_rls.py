"""embedding_cache (migration 0013): per-org FORCE RLS, hash+vector only, eviction."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from m01_seed import add_member, insert_org, insert_user
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.db import set_tenant_context
from app.modules.ai.embeddings.cache import SqlEmbeddingCacheStore, evict_org, text_key

SPACE = "space-test"


def _factory(engine: Engine):  # type: ignore[no-untyped-def]
    @contextmanager
    def open_session(org_id: uuid.UUID) -> Iterator[Session]:
        session = Session(engine)
        try:
            with session.begin():
                set_tenant_context(session, user_id=None, org_id=org_id)
                yield session
        finally:
            session.close()

    return open_session


@pytest.fixture
def orgs(admin_engine: Engine, migrated: None) -> tuple[uuid.UUID, uuid.UUID]:
    with admin_engine.begin() as conn:
        owner = insert_user(conn)
        made = []
        for _ in range(2):
            org = insert_org(conn, created_by=owner)
            add_member(conn, org, owner, "owner")
            made.append(org)
        return made[0], made[1]


def test_round_trip_is_per_org_and_stores_no_text(
    app_engine: Engine, admin_engine: Engine, orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_a, org_b = orgs
    key = text_key("some secret chunk text")
    SqlEmbeddingCacheStore(_factory(app_engine), org_a).put_many(SPACE, {key: [0.5, -0.25, 1.0]})
    assert SqlEmbeddingCacheStore(_factory(app_engine), org_a).get_many(SPACE, [key]) == {
        key: [0.5, -0.25, 1.0]
    }
    # Another org sees nothing; the same hash in another space misses too.
    assert SqlEmbeddingCacheStore(_factory(app_engine), org_b).get_many(SPACE, [key]) == {}
    assert SqlEmbeddingCacheStore(_factory(app_engine), org_a).get_many("other", [key]) == {}
    with admin_engine.connect() as conn:
        cols = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'embedding_cache'"
                )
            )
        ]
    assert "text" not in cols
    assert "content" not in cols


def test_forged_org_insert_is_rejected_by_rls(
    app_engine: Engine, orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_a, org_b = orgs
    with pytest.raises(DBAPIError), _factory(app_engine)(org_a) as session:
        session.execute(
            text(
                "INSERT INTO embedding_cache (org_id, embedding_space_id, text_sha256, vector)"
                " VALUES (:o, 's', :k, :v)"
            ),
            {"o": org_b, "k": text_key("x"), "v": b"\x00\x00\x80?"},
        )


def test_evict_removes_old_and_over_limit(
    app_engine: Engine, admin_engine: Engine, orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    org_a, _ = orgs
    store = SqlEmbeddingCacheStore(_factory(app_engine), org_a)
    keys = [text_key(f"t{i}") for i in range(5)]
    store.put_many(SPACE, {k: [1.0] for k in keys})
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE embedding_cache SET last_used_at = now() - interval '200 days'"
                " WHERE org_id = :o AND text_sha256 = :k"
            ),
            {"o": org_a, "k": keys[0]},
        )
    with _factory(app_engine)(org_a) as session:
        assert evict_org(session, max_rows=3, max_age_days=90) == 2  # 1 aged + 1 over the cap
    assert len(store.get_many(SPACE, keys)) == 3
