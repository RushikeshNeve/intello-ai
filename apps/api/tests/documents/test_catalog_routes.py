"""Catalog route audit and pure helpers (M02.02.02 - M02.02.04). No database needed."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from app.main import create_app
from app.modules.documents import catalog_service as svc

DOCS = "/v1/orgs/{org_id}/workspaces/{workspace_id}/documents"

# Every route under ``/documents``, exactly as the contract (m02-contracts.md §5) lists them.
# There is deliberately no POST/PUT/PATCH/DELETE on a version: versions are immutable (ADR-009).
EXPECTED_DOCUMENT_ROUTES = {
    (DOCS, "GET"),
    (f"{DOCS}/{{document_id}}", "GET"),
    (f"{DOCS}/{{document_id}}", "PATCH"),
    (f"{DOCS}/{{document_id}}", "DELETE"),
    (f"{DOCS}/{{document_id}}/versions", "GET"),
    (f"{DOCS}/{{document_id}}/versions/{{version_id}}", "GET"),
    (f"{DOCS}/{{document_id}}/versions/{{version_id}}/content-url", "GET"),
    # M03.03.04 (m03-contracts.md §11): processing status of the latest version.
    (f"{DOCS}/{{document_id}}/ingestion", "GET"),
    (f"{DOCS}/{{document_id}}/ingestion/retry", "POST"),
    (f"{DOCS}/{{document_id}}/ingestion/events", "GET"),
    # M04.01.04 (m04-contracts.md §4): chunk evidence read from the artifact store.
    (f"{DOCS}/{{document_id}}/versions/{{version_id}}/chunks/{{chunk_id}}", "GET"),
}
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _routes_containing(fragment: str) -> set[tuple[str, str]]:
    spec = create_app().openapi()
    return {
        (path, method.upper())
        for path, item in spec["paths"].items()
        if fragment in path
        for method in item
        if method in HTTP_METHODS
    }


def test_documents_routes_are_exactly_the_contract() -> None:
    assert _routes_containing("/documents") == EXPECTED_DOCUMENT_ROUTES


def test_no_route_anywhere_mutates_a_version() -> None:
    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    offenders = {
        (path, method)
        for path, method in _routes_containing("version")
        if method in mutating and "/uploads" not in path
    }
    assert offenders == set()


def _walk(routes: list[Any]) -> Iterator[tuple[str, set[str]]]:
    for route in routes:
        inner = getattr(route, "original_router", None)  # an included router (FastAPI >= 0.13x)
        if inner is not None:
            yield from _walk(list(inner.routes))
        else:
            yield getattr(route, "path", ""), set(getattr(route, "methods", None) or ())


def test_runtime_routes_match_the_openapi_audit() -> None:
    """The OpenAPI document could hide routes (``include_in_schema=False``): walk the router."""
    seen = {
        (path, method)
        for path, methods in _walk(list(create_app().routes))
        if "/documents" in path
        for method in methods
        if method not in {"HEAD", "OPTIONS"}
    }
    assert seen == EXPECTED_DOCUMENT_ROUTES


def test_cursor_round_trip_and_scope() -> None:
    ident = uuid.uuid4()
    token = svc.PageCursor("documents:title:asc", 'Zoë | ☃ "quoted"', ident).encode()
    assert "=" not in token
    decoded = svc.PageCursor.decode(token, "documents:title:asc")
    assert (decoded.key, decoded.id) == ('Zoë | ☃ "quoted"', ident)
    with pytest.raises(ValueError, match="invalid cursor"):
        svc.PageCursor.decode(token, "documents:title:desc")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "!!!",
        "abc",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b"[1,2,3]").decode(),
        base64.urlsafe_b64encode(b'["s","k"]').decode(),
        base64.urlsafe_b64encode(json.dumps(["s", "k", "nope"]).encode()).decode(),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        base64.urlsafe_b64encode(b"[" * 100000 + b"]" * 100000).decode(),
    ],
)
def test_garbage_cursors_raise_value_error(token: str) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        svc.PageCursor.decode(token, "s")


def test_cursor_keys_are_validated_per_sort() -> None:
    ident = uuid.uuid4()
    svc.validate_cursor_key("title", svc.PageCursor("s", "anything", ident))
    svc.validate_cursor_key("updated_at", svc.PageCursor("s", "2026-01-01T00:00:00+00:00", ident))
    for bad in ("nope", "2026-01-01T00:00:00"):
        with pytest.raises(ValueError, match=r"."):
            svc.validate_cursor_key("created_at", svc.PageCursor("s", bad, ident))


def test_like_wildcards_are_escaped() -> None:
    assert svc._like_escape("100%_\\") == "100\\%\\_\\\\"
    assert svc._like_escape("plain") == "plain"
