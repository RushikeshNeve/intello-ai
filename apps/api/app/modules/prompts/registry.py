"""``resolve(kind, org_id)``: the single entry the chat, graph and research callers use.

Returns the org's active custom body, else the global default (seeded from the repo file on first
use), else - when no session is given or the database fails - the repo default itself. The result
carries the version id (``None`` for the file fallback) and the body hash, for run metadata.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from functools import cache
from pathlib import Path
from typing import Final, NamedTuple

from sqlalchemy import text
from sqlalchemy.orm import Session

KINDS: Final = ("chat_system", "graph_extract", "research_plan")
_DEFAULTS: Final = Path(__file__).parent / "defaults"
_log: Final = logging.getLogger(__name__)


class Resolved(NamedTuple):
    body: str
    version_id: uuid.UUID | None
    sha256: str


def sha256_of(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@cache
def default_body(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown prompt kind: {kind}")
    return (_DEFAULTS / f"{kind}.txt").read_text(encoding="utf-8").strip()


def _fallback(kind: str) -> Resolved:
    body = default_body(kind)
    return Resolved(body, None, sha256_of(body))


def resolve(kind: str, org_id: uuid.UUID | None, session: Session | None = None) -> Resolved:
    """Never raises for storage problems: the repo default is always a valid answer."""
    fallback = _fallback(kind)
    if session is None:
        return fallback
    try:
        with session.begin_nested():
            global_id = session.execute(
                text("SELECT prompt_seed_default(:k, :b, :h)"),
                {"k": kind, "b": fallback.body, "h": fallback.sha256},
            ).scalar_one()
            row = None
            if org_id is not None:
                row = session.execute(
                    text(
                        "SELECT id, body, sha256 FROM prompt_versions"
                        " WHERE org_id = :o AND kind = :k AND state = 'active'"
                    ),
                    {"o": org_id, "k": kind},
                ).one_or_none()
    except Exception:  # noqa: BLE001 - any storage problem means "use the repo default"
        _log.warning("prompt registry unavailable; using the repo default", extra={"kind": kind})
        return fallback
    if row is not None and sha256_of(row.body) == row.sha256:
        return Resolved(row.body, row.id, row.sha256)
    return Resolved(fallback.body, global_id, fallback.sha256)
