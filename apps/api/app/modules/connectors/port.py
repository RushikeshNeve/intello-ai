"""The connector port (M06.01.01; contract m06 section 2).

A connector only **discovers and fetches**; everything fetched is stored through the shared
document path (``documents.remote_ingest``), never by the connector itself.

Auth contract: a token is optional (no token = public access) and must be a minimal, read-only
credential (GitHub: fine-grained token with ``Contents: read`` and ``Metadata: read`` on the one
repository). It is decrypted only inside the sync task and handed to the connector's HTTP client; a
connector never logs, stores or returns it.

Cursor contract: opaque, monotonic per source. ``discover(None)`` is a full listing; replaying a
cursor is safe (the item map keyed by ``(source_id, remote_id)`` + content sha256 absorbs it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ItemSkipped(Exception):
    """``fetch`` declines one item (too large, binary...); the sync counts it and moves on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConnectorError(Exception):
    """A sync failure with a stable, secret-free ``code`` (``source_syncs.error_code``)."""

    def __init__(self, code: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class RemoteItem:
    remote_id: str
    path: str
    version_token: str
    size: int
    content_type: str | None = None
    deleted: bool = False


@dataclass(frozen=True)
class DeltaPage:
    items: list[RemoteItem]
    next_cursor: str | None
    has_more: bool
    # A complete listing: active items the page does not mention no longer exist upstream.
    full_listing: bool = False


@dataclass(frozen=True)
class FetchedItem:
    data: bytes
    content_type: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourceConnector(Protocol):
    def validate(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Normalise and validate a source config; raises ``ValueError`` (message is user-safe)."""
        ...

    def discover(self, cursor: str | None) -> DeltaPage:
        """Changes since ``cursor`` (``None`` = full listing). ``next_cursor`` is only to be
        persisted after every item of the page was committed."""
        ...

    def fetch(self, item: RemoteItem) -> FetchedItem: ...
