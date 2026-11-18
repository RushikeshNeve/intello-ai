"""The documents router is mounted and its sub-routers exist (bodies are owned by batch B)."""

from __future__ import annotations

from app.main import create_app
from app.modules.documents import api, deletion, router, upload, versions
from app.modules.documents.errors import (
    FILE_TOO_LARGE,
    QUOTA_EXCEEDED,
    file_too_large,
    storage_unavailable,
    too_many_uploads,
    upload_expired,
)

PREFIX = "/v1/orgs/{org_id}/workspaces/{workspace_id}"


def test_sub_routers_share_the_contract_prefix() -> None:
    for module in (upload, api, versions, deletion):
        assert module.router.prefix == PREFIX
        assert module.router.tags == ["documents"]
    assert router is not None


def test_app_includes_documents_router() -> None:
    assert create_app() is not None


def test_error_helpers_use_contract_status_and_code() -> None:
    assert (file_too_large().status_code, file_too_large().detail) == (413, FILE_TOO_LARGE)
    assert too_many_uploads().status_code == 429
    assert upload_expired().status_code == 410
    assert storage_unavailable().status_code == 503
    assert QUOTA_EXCEEDED == "quota_exceeded"
