"""Retrieval result cache (M11.04): the gathered (fused) candidates of a query, short TTL.

Key: (org, user, fingerprint of the user's workspace set, query hash, mode, limits, embedding space,
corpus version). The corpus version is a per-org counter bumped on every document create /
supersede / delete and every grant change (:func:`bump_corpus`); it lives in Redis when reachable
(the ingestion worker and the API are different processes) else in this process. Entries live in
this process only (they hold chunk text; no serialization). A hit only skips the index/embedder
work: ``service.finish`` still revalidates every candidate against PostgreSQL (liveness and
authorization), so a stale entry can never surface a deleted or forbidden chunk.

The flag ``RETRIEVAL_CACHE_ENABLED`` defaults OFF; with it off nothing here does any work.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.modules.retrieval.scope import RetrievalScope
    from app.modules.retrieval.service import Gathered


class RetrievalCacheSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    RETRIEVAL_CACHE_ENABLED: bool = False
    RETRIEVAL_CACHE_TTL_SECONDS: float = Field(default=30.0, gt=0, le=3600)
    RETRIEVAL_CACHE_MAX_ENTRIES: int = Field(default=1000, ge=1)


@lru_cache(maxsize=1)
def get_cache_settings() -> RetrievalCacheSettings:
    return RetrievalCacheSettings()


class VersionBackend(Protocol):
    def get(self, org_id: uuid.UUID) -> int | None: ...

    def bump(self, org_id: uuid.UUID) -> None: ...


class MemoryVersions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._v: dict[uuid.UUID, int] = {}

    def get(self, org_id: uuid.UUID) -> int | None:
        with self._lock:
            return self._v.get(org_id, 0)

    def bump(self, org_id: uuid.UUID) -> None:
        with self._lock:
            self._v[org_id] = self._v.get(org_id, 0) + 1


class RedisVersions:
    """Shared counter; any Redis error answers ``None`` (the cache is bypassed, never wrong)."""

    def __init__(self, url: str) -> None:
        import redis

        self._r: Any = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)

    @staticmethod
    def _key(org_id: uuid.UUID) -> str:
        return f"ukos:corpus_version:{org_id}"

    def get(self, org_id: uuid.UUID) -> int | None:
        try:
            raw = self._r.get(self._key(org_id))
            return int(raw) if raw is not None else 0
        except Exception:  # noqa: BLE001 - bypass the cache on any Redis problem
            return None

    def bump(self, org_id: uuid.UUID) -> None:
        with contextlib.suppress(Exception):  # TTL bounds the staleness
            self._r.incr(self._key(org_id))


_versions: VersionBackend | None = None
_lock = threading.Lock()
_entries: OrderedDict[tuple[Any, ...], tuple[float, Gathered]] = OrderedDict()
_clock: Callable[[], float] = time.monotonic


def set_version_backend(backend: VersionBackend | None) -> None:
    """Test hook / explicit wiring. ``None`` resets to the lazy default."""
    global _versions
    _versions = backend


def clear() -> None:
    with _lock:
        _entries.clear()


def _backend() -> VersionBackend:
    global _versions
    if _versions is None:
        try:
            from app.core.config import get_settings

            _versions = RedisVersions(get_settings().REDIS_URL.get_secret_value())
        except Exception:  # noqa: BLE001 - no Redis: in-process counter
            _versions = MemoryVersions()
    return _versions


def bump_corpus(org_id: uuid.UUID) -> None:
    """Invalidate the org's cached results (call on document/grant changes). No-op when off."""
    if get_cache_settings().RETRIEVAL_CACHE_ENABLED:
        _backend().bump(org_id)


def bump_from_session(session: Session) -> None:
    """Same, for callers that only have the tenant session (org read from the RLS context)."""
    if not get_cache_settings().RETRIEVAL_CACHE_ENABLED:
        return
    org = session.execute(
        text("SELECT nullif(current_setting('app.current_org_id', true), '')")
    ).scalar()
    if org:
        _backend().bump(uuid.UUID(str(org)))


def make_key(
    scope: RetrievalScope,
    query: str,
    *,
    space: str,
    mode: str,
    candidates: int,
    max_per_document: int,
    version: int,
) -> tuple[Any, ...]:
    workspaces = hashlib.sha256(
        ",".join(sorted(str(w) for w in scope.workspace_ids)).encode()
    ).hexdigest()
    qhash = hashlib.sha256(" ".join(query.split()).encode("utf-8")).hexdigest()
    return (scope.org_id, scope.user_id, workspaces, qhash, space, mode, candidates,
            max_per_document, version)  # fmt: skip


def lookup(scope: RetrievalScope, query: str, **parts: Any) -> tuple[tuple[Any, ...] | None, Any]:
    """``(key, gathered_or_None)``; ``key`` is None when the cache must be bypassed."""
    settings = get_cache_settings()
    if not settings.RETRIEVAL_CACHE_ENABLED:
        return None, None
    version = _backend().get(scope.org_id)
    if version is None:
        return None, None
    key = make_key(scope, query, version=version, **parts)
    with _lock:
        entry = _entries.get(key)
        if entry is not None:
            if _clock() - entry[0] <= settings.RETRIEVAL_CACHE_TTL_SECONDS:
                _entries.move_to_end(key)
                return key, entry[1]
            del _entries[key]
    return key, None


def store(key: tuple[Any, ...] | None, gathered: Gathered) -> None:
    if key is None or gathered.degraded:  # never pin a degraded answer
        return
    with _lock:
        _entries[key] = (_clock(), gathered)
        while len(_entries) > get_cache_settings().RETRIEVAL_CACHE_MAX_ENTRIES:
            _entries.popitem(last=False)
