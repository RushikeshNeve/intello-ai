"""Shared helpers for the processor/chunking tests (no tests in here).

``block_network`` makes any socket use fail *and records the attempt*: processors catch broad
exceptions around parsers, so a raised error alone could be swallowed; tests assert the attempt list
is empty. The autouse-style fixture ``no_network`` applies it to a whole test module.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.modules.ingestion.contracts import ExtractedDocument, SourceInfo
from app.modules.ingestion.processors import DEFAULT_LIMITS, Limits, extract

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ingestion"
INPUTS = FIXTURES / "inputs"
GOLDEN = FIXTURES / "golden"

VERSION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
REVISION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
GOLDEN_CHUNK_PARAMS: dict[str, int] = {"target_chars": 300, "overlap_chars": 50, "max_chars": 500}

TEXT = "text/plain"
MARKDOWN = "text/markdown"
HTML = "text/html"
PDF = "application/pdf"


def source_for(data: bytes, content_type: str) -> SourceInfo:
    return SourceInfo(
        version_id=VERSION_ID,
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=content_type or "application/x-unknown",
        byte_size=len(data),
    )


@contextlib.contextmanager
def block_network() -> Iterator[list[str]]:
    """Fail every socket operation; yields the list of attempted operations (should stay empty)."""
    attempts: list[str] = []

    def refuse(name: str) -> Any:
        def _refuse(*args: object, **kwargs: object) -> Any:
            attempts.append(name)
            raise OSError(f"network access attempted: {name}")

        return _refuse

    originals: list[tuple[object, str, object]] = []
    targets: list[tuple[object, str]] = [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket.socket, "sendto"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket, "gethostbyname"),
        (socket, "gethostbyname_ex"),
    ]
    for owner, attr in targets:
        originals.append((owner, attr, getattr(owner, attr)))
        setattr(owner, attr, refuse(f"{getattr(owner, '__name__', 'socket')}.{attr}"))
    try:
        yield attempts
    finally:
        for owner, attr, original in originals:
            setattr(owner, attr, original)


@pytest.fixture
def no_network() -> Iterator[None]:
    with block_network() as attempts:
        yield
    assert attempts == [], f"processor attempted network access: {attempts}"


def run(data: bytes, content_type: str, limits: Limits | None = None) -> ExtractedDocument:
    """Extract with network use blocked and asserted absent."""
    with block_network() as attempts:
        doc = extract(
            data,
            content_type,
            source=source_for(data, content_type),
            limits=limits or DEFAULT_LIMITS,
        )
    assert attempts == []
    return doc


def update_golden() -> bool:
    return os.environ.get("UPDATE_GOLDEN") == "1"


def pretty_json(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check_golden(path: Path, actual: object) -> None:
    """Compare ``actual`` (JSON-able) with the golden file; ``UPDATE_GOLDEN=1`` rewrites it."""
    if update_golden():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pretty_json(actual), encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"missing golden file {path}; run with UPDATE_GOLDEN=1 to create it"
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"{path.name} differs from the golden file. If the change is intended, regenerate with "
        "UPDATE_GOLDEN=1 and review the diff."
    )
