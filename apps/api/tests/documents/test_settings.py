"""Upload/quota settings defaults and bounds (contract m02-contracts.md §3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults_match_contract() -> None:
    s = Settings()
    assert s.UPLOAD_MAX_BYTES == 104_857_600
    assert s.ORG_STORAGE_QUOTA_BYTES == 5_368_709_120
    assert s.UPLOAD_SESSION_TTL_SECONDS == 3600
    assert s.UPLOAD_MAX_PENDING_PER_USER == 20
    assert s.DOCUMENT_PURGE_GRACE_DAYS == 7


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "1024")
    monkeypatch.setenv("DOCUMENT_PURGE_GRACE_DAYS", "30")
    s = Settings()
    assert (s.UPLOAD_MAX_BYTES, s.DOCUMENT_PURGE_GRACE_DAYS) == (1024, 30)


@pytest.mark.parametrize(
    "name",
    [
        "UPLOAD_MAX_BYTES",
        "ORG_STORAGE_QUOTA_BYTES",
        "UPLOAD_SESSION_TTL_SECONDS",
        "UPLOAD_MAX_PENDING_PER_USER",
        "DOCUMENT_PURGE_GRACE_DAYS",
    ],
)
@pytest.mark.parametrize("bad", ["0", "-1", "abc", "99999999999999999999999"])
def test_invalid_values_rejected(monkeypatch: pytest.MonkeyPatch, name: str, bad: str) -> None:
    monkeypatch.setenv(name, bad)
    with pytest.raises(ValidationError):
        Settings()
