"""Embedding cache (M11.03, no database): the store is an in-memory fake of the SQL table."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.modules.ai.embeddings import EmbeddingUnavailable, FakeEmbeddingClient
from app.modules.ai.embeddings import cache as ec
from app.modules.ai.embeddings.registry import DEFAULT_SPEC

Rows = dict[tuple[uuid.UUID, str, str], list[float]]


class MemStore:
    """Mimics the per-org table: rows keyed (org, space, hash)."""

    def __init__(self, org: uuid.UUID, rows: Rows) -> None:
        self.org, self.rows = org, rows

    def get_many(self, space_id: str, keys: list[str]) -> dict[str, list[float]]:
        return {
            k: self.rows[(self.org, space_id, k)]
            for k in keys
            if (self.org, space_id, k) in self.rows
        }

    def put_many(self, space_id: str, items: dict[str, list[float]]) -> None:
        for k, v in items.items():
            self.rows[(self.org, space_id, k)] = v


def test_unchanged_chunks_make_zero_embedder_calls_and_org_and_space_isolate() -> None:
    rows: Rows = {}
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    texts = ["alpha chunk text", "beta chunk text", "alpha chunk text"]

    fake = FakeEmbeddingClient()
    first = ec.CachingEmbedder(fake, MemStore(org_a, rows), org_id=org_a).embed_documents(texts)
    assert fake.document_calls == 1
    assert first[0] == first[2]
    assert len(first) == 3

    again = ec.CachingEmbedder(fake, MemStore(org_a, rows), org_id=org_a).embed_documents(texts)
    assert fake.document_calls == 1  # re-ingest of unchanged text: 0 embedder calls
    assert again == first

    ec.CachingEmbedder(fake, MemStore(org_b, rows), org_id=org_b).embed_documents(texts)
    assert fake.document_calls == 2  # another org gets no hit

    new_spec = dataclasses.replace(DEFAULT_SPEC, spec_version=DEFAULT_SPEC.spec_version + 1)
    other_space = FakeEmbeddingClient(new_spec)
    ec.CachingEmbedder(other_space, MemStore(org_a, rows), org_id=org_a).embed_documents(texts)
    assert other_space.document_calls == 1  # a space change misses
    assert all(len(k[2]) == 64 for k in rows)  # hashes only, never text


def test_embedder_failure_propagates_and_caches_nothing() -> None:
    rows: Rows = {}
    org = uuid.uuid4()
    fake = FakeEmbeddingClient()
    fake.fail_next(EmbeddingUnavailable("down"))
    with pytest.raises(EmbeddingUnavailable):
        ec.CachingEmbedder(fake, MemStore(org, rows), org_id=org).embed_documents(["x y"])
    assert rows == {}


def test_query_cache_hits_per_org_and_flag_off_passes_through() -> None:
    ec.clear_query_cache()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    fake = FakeEmbeddingClient()
    v1 = ec.embed_query_cached(fake, org_a, "what is x", enabled=True)
    v2 = ec.embed_query_cached(fake, org_a, "what is x", enabled=True)
    assert v1 == v2
    assert fake.query_calls == 1
    ec.embed_query_cached(fake, org_b, "what is x", enabled=True)
    assert fake.query_calls == 2
    ec.embed_query_cached(fake, org_a, "what is x", enabled=False)
    assert fake.query_calls == 3
    ec.clear_query_cache()


def test_evict_all_is_a_noop_while_disabled_and_skips_failing_orgs() -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from sqlalchemy.orm import Session

    orgs = [uuid.uuid4(), uuid.uuid4()]
    opened: list[uuid.UUID] = []

    @contextmanager
    def factory(org: uuid.UUID) -> Iterator[Session]:
        opened.append(org)
        raise RuntimeError("boom")
        yield  # pragma: no cover

    off = ec.EmbeddingCacheSettings(EMBEDDING_CACHE_ENABLED=False)
    assert ec.evict_all(lambda n: orgs, factory, settings=off) == 0
    assert opened == []  # disabled: nothing is even listed
    on = ec.EmbeddingCacheSettings(EMBEDDING_CACHE_ENABLED=True)
    assert ec.evict_all(lambda n: orgs, factory, settings=on) == 0
    assert opened == orgs  # a failing org does not stop the sweep


def test_dispatcher_queues_eviction_only_when_cache_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import dispatcher
    from app.workers.celery_app import TASK_CACHE_EVICT

    sent: list[dispatcher.Delivery] = []
    d = dispatcher.Dispatcher.__new__(dispatcher.Dispatcher)
    d.publish = sent.append
    monkeypatch.setenv("EMBEDDING_CACHE_ENABLED", "false")
    ec.get_cache_settings.cache_clear()
    assert d.cache_evict_tick() is False
    assert sent == []
    monkeypatch.setenv("EMBEDDING_CACHE_ENABLED", "true")
    ec.get_cache_settings.cache_clear()
    try:
        assert d.cache_evict_tick() is True
        assert [x.task for x in sent] == [TASK_CACHE_EVICT]
    finally:
        ec.get_cache_settings.cache_clear()
