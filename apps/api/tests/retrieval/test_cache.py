"""Retrieval result cache (M11.04, no database): invalidation, scope isolation, TTL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from app.modules.ai.embeddings import FakeEmbeddingClient
from app.modules.retrieval import cache, finish, gather
from tests.retrieval.support import FakeDb, index_document, make_scope, new_repo, org_scope, pub_row


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RETRIEVAL_CACHE_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_CACHE_TTL_SECONDS", "30")
    cache.get_cache_settings.cache_clear()
    cache.set_version_backend(cache.MemoryVersions())
    cache.clear()
    yield
    cache.get_cache_settings.cache_clear()
    cache.set_version_backend(None)
    cache.clear()


class CountingRepo:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def search_keyword(self, *a: Any, **k: Any) -> Any:
        self.calls += 1
        return self._inner.search_keyword(*a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _world() -> tuple[uuid.UUID, uuid.UUID, FakeEmbeddingClient]:
    return uuid.uuid4(), uuid.uuid4(), FakeEmbeddingClient()


def test_hit_skips_index_and_deletion_never_returns_deleted_doc() -> None:
    org_id, w, emb = _world()
    org = org_scope(org_id)
    inner = new_repo(org, emb)
    repo: Any = CountingRepo(inner)
    a = index_document(inner, org, emb, w, ["shared keyword alpha"])
    b = index_document(inner, org, emb, w, ["shared keyword beta"])
    scope = make_scope(org_id, [w])
    first = gather(scope, repo, emb, "shared keyword", candidates=20)
    calls = repo.calls
    again = gather(scope, repo, emb, "shared  keyword", candidates=20)
    assert again is first
    assert repo.calls == calls  # served from the cache

    # `a` is deleted in PostgreSQL (no published row): the cached candidates still hold it, but
    # the unchanged revalidation drops it.
    db = FakeDb(org_id=org_id, published=[pub_row(b, w)])
    res = finish(db.session, scope, again, top_k=5)
    assert [e.document_id for e in res.evidence] == [b.document_id]
    assert a.document_id not in {e.document_id for e in res.evidence}

    cache.bump_corpus(org_id)  # the delete hook
    gather(scope, repo, emb, "shared keyword", candidates=20)
    assert repo.calls > calls  # recomputed


def test_other_user_scope_never_shares_an_entry() -> None:
    org_id, w, emb = _world()
    org = org_scope(org_id)
    inner = new_repo(org, emb)
    repo: Any = CountingRepo(inner)
    index_document(inner, org, emb, w, ["shared keyword alpha"])
    s1 = make_scope(org_id, [w])
    s2 = make_scope(org_id, [w])  # same workspaces, different user
    gather(s1, repo, emb, "shared keyword")
    calls = repo.calls
    gather(s2, repo, emb, "shared keyword")
    assert repo.calls > calls
    # same user, different workspace set: miss as well
    s3 = make_scope(org_id, [w, uuid.uuid4()], user_id=s1.user_id)
    calls = repo.calls
    gather(s3, repo, emb, "shared keyword")
    assert repo.calls > calls


def test_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id, w, emb = _world()
    org = org_scope(org_id)
    inner = new_repo(org, emb)
    repo: Any = CountingRepo(inner)
    index_document(inner, org, emb, w, ["shared keyword alpha"])
    scope = make_scope(org_id, [w])
    now = [1000.0]
    monkeypatch.setattr(cache, "_clock", lambda: now[0])
    gather(scope, repo, emb, "shared keyword")
    calls = repo.calls
    now[0] += 29
    gather(scope, repo, emb, "shared keyword")
    assert repo.calls == calls
    now[0] += 2
    gather(scope, repo, emb, "shared keyword")
    assert repo.calls > calls


def test_flag_off_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_CACHE_ENABLED", "false")
    cache.get_cache_settings.cache_clear()
    org_id, w, emb = _world()
    org = org_scope(org_id)
    inner = new_repo(org, emb)
    repo: Any = CountingRepo(inner)
    index_document(inner, org, emb, w, ["shared keyword alpha"])
    scope = make_scope(org_id, [w])
    gather(scope, repo, emb, "shared keyword")
    calls = repo.calls
    gather(scope, repo, emb, "shared keyword")
    assert repo.calls == 2 * calls
