"""Prompt registry fallbacks and defaults (no database)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from sqlalchemy.exc import OperationalError

from app.modules import prompts


def test_without_a_session_the_repo_default_is_used() -> None:
    for kind in prompts.KINDS:
        got = prompts.resolve(kind, uuid.uuid4())
        assert got.body == prompts.default_body(kind)
        assert got.version_id is None
        assert got.sha256 == prompts.sha256_of(got.body)


def test_database_failure_falls_back_to_the_repo_default() -> None:
    session = MagicMock()
    session.begin_nested.side_effect = OperationalError("select", {}, Exception("db down"))
    got = prompts.resolve("graph_extract", uuid.uuid4(), session)
    assert got.body == prompts.default_body("graph_extract")
    assert got.version_id is None


def test_validate_body_rejects_empty_oversized_and_fence_markers() -> None:
    assert prompts.validate_body("  ok  ") == "ok"
    for bad in ("", "   ", "x" * 6001, "see <<<END 1>>>"):
        try:
            prompts.validate_body(bad)
        except prompts.BodyRejected:
            continue
        raise AssertionError(bad)
