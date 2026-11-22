"""Embedding cache (M11.03): unchanged chunks and repeated queries make 0 embedder calls.

* Documents: ``CachingEmbedder`` wraps any ``EmbeddingProvider``; ``embed_documents_detailed``
  looks up ``sha256(NFC text)`` per (org, embedding space) in the store and sends only the misses
  to the inner embedder. The store holds hash + vector, NEVER the text. The SQL store runs in the
  org's tenant session (table ``embedding_cache``, FORCE RLS by org: see migration 0013 for why the
  cache is per org and not global).
* Queries: a small in-process LRU keyed (org, space, hash); queries are short-lived and never
  persisted.
* The embedding space id (model, dimensions, prefixes, spec version) is part of the key, so a
  space change always misses. The flag ``EMBEDDING_CACHE_ENABLED`` defaults OFF.
"""

from __future__ import annotations

import hashlib
import struct
import threading
import unicodedata
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import lru_cache
from typing import Any, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from app.modules.ai.embeddings.ports import EmbeddingBatchResult, EmbeddingProvider, EmbeddingUsage
from app.modules.ai.embeddings.registry import EmbeddingModelSpec


class EmbeddingCacheSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    EMBEDDING_CACHE_ENABLED: bool = False
    EMBEDDING_CACHE_MAX_ROWS_PER_ORG: int = Field(default=200_000, ge=1)
    EMBEDDING_CACHE_MAX_AGE_DAYS: int = Field(default=90, ge=1)
    EMBEDDING_QUERY_CACHE_SIZE: int = Field(default=512, ge=1)
    EMBEDDING_CACHE_EVICT_INTERVAL_SECONDS: int = Field(default=3600, ge=60)
    EMBEDDING_CACHE_EVICT_MAX_ORGS: int = Field(default=1000, ge=1)


@lru_cache(maxsize=1)
def get_cache_settings() -> EmbeddingCacheSettings:
    return EmbeddingCacheSettings()


def text_key(value: str) -> str:
    """Cache key of a text: sha256 of its NFC form (the text itself is never stored)."""
    return hashlib.sha256(unicodedata.normalize("NFC", value).encode("utf-8")).hexdigest()


def pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(data: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(data) // 4}f", data))


class EmbeddingCacheStore(Protocol):
    def get_many(self, space_id: str, keys: list[str]) -> dict[str, list[float]]: ...

    def put_many(self, space_id: str, items: dict[str, list[float]]) -> None: ...


class SqlEmbeddingCacheStore:
    """The ``embedding_cache`` table through an org-scoped session factory (``session_factory(org)``
    opens a transaction with ``app.current_org_id`` set, like the ingestion worker's)."""

    def __init__(
        self,
        session_factory: Callable[[uuid.UUID], AbstractContextManager[Session]],
        org_id: uuid.UUID,
    ) -> None:
        self._factory = session_factory
        self._org = org_id

    def get_many(self, space_id: str, keys: list[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        with self._factory(self._org) as session:
            rows = session.execute(
                text(
                    "SELECT text_sha256, vector FROM embedding_cache"
                    " WHERE org_id = :o AND embedding_space_id = :s AND text_sha256 = ANY(:k)"
                ),
                {"o": self._org, "s": space_id, "k": keys},
            ).all()
            found = {r[0]: unpack(bytes(r[1])) for r in rows}
            if found:
                session.execute(
                    text(
                        "UPDATE embedding_cache SET last_used_at = now()"
                        " WHERE org_id = :o AND embedding_space_id = :s AND text_sha256 = ANY(:k)"
                    ),
                    {"o": self._org, "s": space_id, "k": list(found)},
                )
        return found

    def put_many(self, space_id: str, items: dict[str, list[float]]) -> None:
        if not items:
            return
        with self._factory(self._org) as session:
            for key, vector in items.items():
                session.execute(
                    text(
                        "INSERT INTO embedding_cache"
                        " (org_id, embedding_space_id, text_sha256, vector)"
                        " VALUES (:o, :s, :k, :v) ON CONFLICT DO NOTHING"
                    ),
                    {"o": self._org, "s": space_id, "k": key, "v": pack(vector)},
                )


def evict_org(session: Session, *, max_rows: int, max_age_days: int) -> int:
    """Delete the context org's entries older than ``max_age_days`` (by last use), then the least
    recently used ones beyond ``max_rows``. Run per org inside its tenant session (RLS limits the
    statements to that org). Returns rows deleted."""
    aged: CursorResult[Any] = session.execute(  # type: ignore[assignment]
        text("DELETE FROM embedding_cache WHERE last_used_at < now() - make_interval(days => :d)"),
        {"d": max_age_days},
    )
    over: CursorResult[Any] = session.execute(  # type: ignore[assignment]
        text(
            "DELETE FROM embedding_cache c USING ("
            " SELECT org_id, embedding_space_id, text_sha256 FROM embedding_cache"
            " ORDER BY last_used_at DESC OFFSET :n) old"
            " WHERE c.org_id = old.org_id AND c.embedding_space_id = old.embedding_space_id"
            " AND c.text_sha256 = old.text_sha256"
        ),
        {"n": max_rows},
    )
    return int(aged.rowcount or 0) + int(over.rowcount or 0)


def evict_all(
    list_orgs: Callable[[int], list[uuid.UUID]],
    session_factory: Callable[[uuid.UUID], AbstractContextManager[Session]],
    *,
    settings: EmbeddingCacheSettings | None = None,
) -> int:
    """Periodic maintenance: :func:`evict_org` for every org holding cache rows, one tenant
    session each (a failing org is skipped, not fatal). No-op (0) while the cache is disabled."""
    cfg = settings or get_cache_settings()
    if not cfg.EMBEDDING_CACHE_ENABLED:
        return 0
    deleted = 0
    for org in list_orgs(cfg.EMBEDDING_CACHE_EVICT_MAX_ORGS):
        try:
            with session_factory(org) as session:
                deleted += evict_org(
                    session,
                    max_rows=cfg.EMBEDDING_CACHE_MAX_ROWS_PER_ORG,
                    max_age_days=cfg.EMBEDDING_CACHE_MAX_AGE_DAYS,
                )
        except Exception:  # noqa: BLE001, S112 - one org must not stop the sweep
            continue
    return deleted


class CachingEmbedder:
    """``EmbeddingProvider`` that answers document embeddings from the store when it can."""

    def __init__(
        self, inner: EmbeddingProvider, store: EmbeddingCacheStore, *, org_id: uuid.UUID
    ) -> None:
        self._inner = inner
        self._store = store
        self._org = org_id

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._inner.spec

    @property
    def usage(self) -> EmbeddingUsage:
        return self._inner.usage

    def embed_documents_detailed(self, texts: list[str]) -> EmbeddingBatchResult:
        space = self._inner.spec.space_id
        keys = [text_key(t) for t in texts]
        hits = self._store.get_many(space, list(dict.fromkeys(keys)))
        miss_idx = [i for i, k in enumerate(keys) if k not in hits]
        if not miss_idx:
            return EmbeddingBatchResult([hits[k] for k in keys], (), EmbeddingUsage())
        # Distinct missing texts only (a repeated chunk is embedded once).
        first: dict[str, int] = {}
        for i in miss_idx:
            first.setdefault(keys[i], i)
        order = list(first)
        result = self._inner.embed_documents_detailed([texts[first[k]] for k in order])
        fresh = dict(zip(order, result.vectors, strict=True))
        self._store.put_many(space, fresh)
        vectors = [hits[k] if k in hits else fresh[k] for k in keys]
        truncated = tuple(i for i in miss_idx if order.index(keys[i]) in result.truncated)
        return EmbeddingBatchResult(vectors, truncated, result.usage)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents_detailed(texts).vectors

    def embed_query(self, text: str) -> list[float]:
        return embed_query_cached(self._inner, self._org, text, enabled=True)

    def close(self) -> None:
        self._inner.close()


# ------------------------------------------------------------------------------------ queries

_QUERY_LOCK = threading.Lock()
_QUERY_CACHE: OrderedDict[tuple[uuid.UUID, str, str], list[float]] = OrderedDict()


def clear_query_cache() -> None:
    with _QUERY_LOCK:
        _QUERY_CACHE.clear()


def embed_query_cached(
    embedder: EmbeddingProvider, org_id: uuid.UUID, query: str, *, enabled: bool | None = None
) -> list[float]:
    """``embedder.embed_query(query)``, memoized per (org, space, query hash) when enabled
    (default: ``EMBEDDING_CACHE_ENABLED``)."""
    settings = get_cache_settings()
    if not (settings.EMBEDDING_CACHE_ENABLED if enabled is None else enabled):
        return embedder.embed_query(query)
    key = (org_id, embedder.spec.space_id, text_key(query))
    with _QUERY_LOCK:
        hit = _QUERY_CACHE.get(key)
        if hit is not None:
            _QUERY_CACHE.move_to_end(key)
            return list(hit)
    vector = embedder.embed_query(query)
    with _QUERY_LOCK:
        _QUERY_CACHE[key] = list(vector)
        while len(_QUERY_CACHE) > settings.EMBEDDING_QUERY_CACHE_SIZE:
            _QUERY_CACHE.popitem(last=False)
    return vector
