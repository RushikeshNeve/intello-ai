"""Helpers for the object-store contract suite (not in conftest.py so tests can import them)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx
from compose_support import ComposeProject  # type: ignore[import-not-found,unused-ignore]

from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.modules.storage.ports import ObjectStore, PresignedRequest


@dataclass(frozen=True)
class Stack:
    compose: ComposeProject
    settings: ObjectStoreSettings
    web_origin: str


@dataclass
class BrowserResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class Browser:
    """Sends presigned requests the way a browser would (httpx for S3, emulation for memory)."""

    def __init__(self, store: ObjectStore, http: httpx.Client) -> None:
        self._store = store
        self._http = http

    def put(
        self, request: PresignedRequest, data: bytes, headers: Mapping[str, str] | None = None
    ) -> BrowserResponse:
        sent = dict(request.headers if headers is None else headers)
        if isinstance(self._store, InMemoryObjectStore):
            r = self._store.simulate_put(request, data, sent)
            return BrowserResponse(r.status, dict(r.headers), r.body)
        resp = self._http.put(request.url, content=data, headers=sent)
        return BrowserResponse(resp.status_code, dict(resp.headers), resp.content)

    def get(self, url: str) -> BrowserResponse:
        if isinstance(self._store, InMemoryObjectStore):
            r = self._store.simulate_get(url)
            return BrowserResponse(r.status, dict(r.headers), r.body)
        resp = self._http.get(url)
        return BrowserResponse(resp.status_code, dict(resp.headers), resp.content)
