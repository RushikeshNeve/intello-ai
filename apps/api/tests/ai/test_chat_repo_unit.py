"""Chat repository: argument validation and shapes that need no database."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.modules.ai import chat_repo as repo


class _NoDb:
    """A session that must never be touched (validation fails first)."""

    def execute(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("the database must not be reached")


FENCE = repo.RunFence(uuid.uuid4(), uuid.uuid4(), "owner", 3)


@pytest.mark.parametrize("state", ["queued", "running", "cancelled", "bogus", ""])
def test_finish_run_only_accepts_completed_or_failed(state: str) -> None:
    with pytest.raises(repo.InvalidTransition):
        repo.finish_run(_NoDb(), FENCE, state=state)  # type: ignore[arg-type]


def test_a_completed_run_has_no_error_code() -> None:
    with pytest.raises(repo.InvalidTransition):
        repo.finish_run(_NoDb(), FENCE, state="completed", error_code="timeout")  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["token", "", "STARTED", "done"])
def test_append_event_rejects_unknown_event_types(kind: str) -> None:
    with pytest.raises(ValueError, match="unknown run event type"):
        repo.append_event(_NoDb(), FENCE, type=kind)  # type: ignore[arg-type]


def test_claimed_run_exposes_its_fence() -> None:
    run, org = uuid.uuid4(), uuid.uuid4()
    claim = repo.ClaimedRun(run, org, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 1, 7, "w@host")
    assert claim.fence == repo.RunFence(run, org, "w@host", 7)


def test_run_limit_error_names_the_limit() -> None:
    err = repo.RunLimitExceeded("per_minute")
    assert err.kind == "per_minute"
    assert isinstance(err, repo.ChatError)


def test_typed_errors_share_a_base() -> None:
    for cls in (repo.ThreadNotFound, repo.RunFenceLost, repo.InvalidTransition):
        assert issubclass(cls, repo.ChatError)


def test_rows_are_immutable() -> None:
    created = repo.CreatedRun(uuid.uuid4(), uuid.uuid4(), True)
    with pytest.raises(AttributeError):
        created.created = False  # type: ignore[misc]
