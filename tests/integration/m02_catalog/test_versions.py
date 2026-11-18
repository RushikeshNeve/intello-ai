"""M02.02.03: version timeline, retention status, content URLs and immutability proofs."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from catalog_support import PRESIGN_TTL, World
from fastapi.testclient import TestClient
from m01_seed import sqlstate
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from tenancy_support import assert_denied

from app.core.db import tenant_session
from app.core.models import INLINE_CONTENT_TYPES, DocumentVersion
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.modules.storage.deps import get_object_store
from app.modules.storage.ports import StorageUnavailable

VERSION_KEYS = {
    "id",
    "document_id",
    "version_number",
    "original_filename",
    "content_type",
    "size_bytes",
    "sha256",
    "created_by",
    "created_at",
    "retention",
}


def put(
    store: InMemoryObjectStore, org_id: uuid.UUID, doc_id: uuid.UUID, version_id: uuid.UUID
) -> str:
    key = f"org/{org_id}/doc/{doc_id}/{version_id}/original"
    data = b"content of " + key.encode()
    store.put_object(
        key,
        data,
        content_type="application/octet-stream",
        sha256_b64=base64.b64encode(hashlib.sha256(data).digest()).decode(),
    )
    return key


def add_version(world: World, document_id: uuid.UUID, number: int, filename: str) -> uuid.UUID:
    """What ``finalize`` does: insert the next version and move ``latest_version_id``."""
    version_id = uuid.uuid4()
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO document_versions (id, org_id, workspace_id, document_id,"
                " version_number, original_filename, content_type, size_bytes, sha256, object_key,"
                " created_by) VALUES (:id, :o, :w, :d, :n, :fn, 'application/pdf', 5, :sha, :k, :u)"
            ),
            {
                "id": version_id,
                "o": world.org_id,
                "w": world.ws,
                "d": document_id,
                "n": number,
                "fn": filename,
                "sha": "c" * 64,
                "k": f"org/{world.org_id}/doc/{document_id}/{version_id}/original",
                "u": world.t.owner.id,
            },
        )
        conn.execute(
            text("UPDATE documents SET latest_version_id = :v, updated_at = now() WHERE id = :d"),
            {"v": version_id, "d": document_id},
        )
    return version_id


# --- timeline -----------------------------------------------------------------------------


def test_timeline_is_newest_first_with_retention_status(world: World) -> None:
    doc = world.seed(title="Evolving", filename="final.pdf", versions=3)
    for name, user in world.everyone.items():
        res = world.api.get(world.doc_path(doc.id, "/versions"), user)
        assert res.status == 200, (name, res.body)
        assert set(res.body) == {"items", "next_cursor"}
        assert res.body["next_cursor"] is None
        items = res.body["items"]
        assert [i["version_number"] for i in items] == [3, 2, 1]
        assert [i["id"] for i in items] == [str(v) for v in reversed(doc.versions)]
        assert [i["retention"] for i in items] == [
            {"status": "current", "purge_after": None},
            {"status": "superseded", "purge_after": None},
            {"status": "superseded", "purge_after": None},
        ]
        for item in items:
            assert set(item) == VERSION_KEYS
            assert item["document_id"] == str(doc.id)
            assert item["sha256"] == "a" * 64
            datetime.fromisoformat(item["created_at"])
        assert items[0]["original_filename"] == "final.pdf"
        assert items[1]["original_filename"] == "v2-final.pdf"


def test_a_new_version_supersedes_without_touching_the_old_one(world: World) -> None:
    doc = world.seed(title="Grows", filename="a.pdf")
    path = world.doc_path(doc.id, "/versions")
    first = world.api.get(path, world.t.owner).body["items"]
    assert [i["retention"]["status"] for i in first] == ["current"]
    second_id = add_version(world, doc.id, 2, "b.pdf")
    after = world.api.get(path, world.t.owner).body["items"]
    assert [(i["version_number"], i["retention"]["status"]) for i in after] == [
        (2, "current"),
        (1, "superseded"),
    ]
    assert after[0]["id"] == str(second_id)
    unchanged = {k: v for k, v in after[1].items() if k != "retention"}
    assert unchanged == {k: v for k, v in first[0].items() if k != "retention"}
    # The document itself reports the new latest version and the count.
    body = world.api.get(world.doc_path(doc.id), world.t.owner).body
    assert body["version_count"] == 2
    assert body["latest_version"]["id"] == str(second_id)
    single = world.api.get(world.doc_path(doc.id, f"/versions/{doc.versions[0]}"), world.t.owner)
    assert single.body["retention"] == {"status": "superseded", "purge_after": None}


def test_timeline_cursor_walk(world: World) -> None:
    doc = world.seed(title="Long", versions=5)
    path = world.doc_path(doc.id, "/versions")
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        res = world.api.get(path, world.t.owner, params=params)
        assert res.status == 200
        pages += 1
        seen.extend(i["version_number"] for i in res.body["items"])
        cursor = res.body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [5, 4, 3, 2, 1]


def test_timeline_cursor_and_limit_validation(world: World) -> None:
    doc = world.seed(title="Long", versions=2)
    path = world.doc_path(doc.id, "/versions")
    document_cursor = world.api.get(
        world.path("/documents"), world.t.owner, params={"limit": 1}
    ).body["next_cursor"]
    for bad in ("garbage", "!!", document_cursor):
        res = world.api.get(path, world.t.owner, params={"cursor": bad or "x"})
        assert (res.status, res.body) == (422, {"detail": "invalid_cursor"})
    for limit in (0, 201):
        res = world.api.get(path, world.t.owner, params={"limit": limit})
        assert (res.status, res.body["detail"]) == (422, "invalid_request")


def test_timeline_and_version_access_rules(world: World) -> None:
    doc = world.seed(title="Guarded", versions=2)
    other = world.seed(title="Other", versions=2)
    version = doc.versions[0]
    paths = [
        world.doc_path(doc.id, "/versions"),
        world.doc_path(doc.id, f"/versions/{version}"),
    ]
    for path in paths:
        res = world.api.get(path, world.plain)
        assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
        res = world.api.get(path, world.t.outsider)
        assert_denied(world.api, res, 404, action="document.read", reason="not_org_member")
        assert world.api.get(path, None).status == 401
        assert world.api.get(path, world.viewer).status == 200
    # A read grant is enough for a user without a workspace role.
    world.grant(doc.id, world.plain, "read")
    for path in paths:
        assert world.api.get(path, world.plain).status == 200
    # ... but only for that document.
    assert world.api.get(world.doc_path(other.id, "/versions"), world.plain).status == 404
    # Expired grant: no access.
    world.grant(other.id, world.other_plain, "read", expires_in="-1 hour")
    assert world.api.get(world.doc_path(other.id, "/versions"), world.other_plain).status == 404


def test_version_ids_of_other_documents_and_orgs_are_404(world: World) -> None:
    doc = world.seed(title="Mine")
    other = world.seed(title="Other")
    foreign = world.seed_foreign(title="Foreign")
    for stray in (other.versions[0], foreign.versions[0], uuid.uuid4()):
        for suffix in ("", "/content-url"):
            res = world.api.get(world.doc_path(doc.id, f"/versions/{stray}{suffix}"), world.t.owner)
            assert (res.status, res.body) == (404, {"detail": "not_found"}), (stray, suffix)
    # A foreign document id never resolves under our workspace.
    for path in (
        world.doc_path(foreign.id, "/versions"),
        world.doc_path(foreign.id, f"/versions/{foreign.versions[0]}"),
        world.doc_path(foreign.id, f"/versions/{foreign.versions[0]}/content-url"),
    ):
        assert world.api.get(path, world.t.owner).status == 404
    # And the foreign owner cannot read ours through their own org.
    res = world.api.get(
        f"/v1/orgs/{world.foreign.org_id}/workspaces/{world.foreign_ws}/documents/{doc.id}"
        f"/versions/{doc.versions[0]}",
        world.foreign.owner,
    )
    assert res.status == 404


def test_get_version_shape(world: World) -> None:
    doc = world.seed(title="One", filename="x.pdf")
    res = world.api.get(world.doc_path(doc.id, f"/versions/{doc.versions[0]}"), world.viewer)
    assert res.status == 200
    assert set(res.body) == VERSION_KEYS
    assert res.body["version_number"] == 1
    assert res.body["size_bytes"] == 100
    assert res.body["retention"] == {"status": "current", "purge_after": None}
    assert "object_key" not in res.body


# --- content URL --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "inline_allowed"),
    [
        ("application/pdf", True),
        ("text/plain", True),
        ("text/markdown", True),
        ("image/png", True),
        ("image/jpeg", True),
        ("image/gif", True),
        ("image/webp", True),
        ("text/plain; charset=utf-8", True),
        ("text/html", False),
        ("TEXT/HTML", False),
        ("application/xhtml+xml", False),
        ("image/svg+xml", False),
        ("application/javascript", False),
        ("application/octet-stream", False),
        ("application/x-unknown", False),
        ("weird/thing", False),
    ],
)
def test_inline_only_for_the_allow_list(
    world: World, store: InMemoryObjectStore, content_type: str, inline_allowed: bool
) -> None:
    doc = world.seed(title="T", filename="doc.bin", content_type=content_type)
    put(store, world.org_id, doc.id, doc.versions[0])
    path = world.doc_path(doc.id, f"/versions/{doc.versions[0]}/content-url")
    asked_inline = world.api.get(path, world.viewer, params={"disposition": "inline"})
    assert asked_inline.status == 200, asked_inline.body
    assert asked_inline.body["disposition"] == ("inline" if inline_allowed else "attachment")
    assert asked_inline.body["content_type"] == content_type
    served = store.simulate_get(asked_inline.body["url"])
    assert served.status == 200
    assert served.headers["content-disposition"].startswith(
        "inline;" if inline_allowed else "attachment;"
    )
    # Attachment is the default and never inline.
    for params in (None, {"disposition": "attachment"}):
        res = world.api.get(path, world.viewer, params=params)
        assert res.body["disposition"] == "attachment"
        assert (
            store.simulate_get(res.body["url"])
            .headers["content-disposition"]
            .startswith("attachment;")
        )


def test_the_allow_list_matches_the_contract() -> None:
    assert {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    } == INLINE_CONTENT_TYPES


def test_content_url_shape_and_signature(world: World, store: InMemoryObjectStore) -> None:
    doc = world.seed(title="Sig", filename="Résumé final.pdf")
    version_id = doc.versions[0]
    key = put(store, world.org_id, doc.id, version_id)
    path = world.doc_path(doc.id, f"/versions/{version_id}/content-url")
    before = datetime.now(UTC)
    res = world.api.get(path, world.editor, params={"disposition": "inline"})
    assert res.status == 200
    assert set(res.body) == {"url", "expires_at", "filename", "content_type", "disposition"}
    assert res.body["filename"] == "Résumé final.pdf"
    assert res.body["content_type"] == "application/pdf"
    url = urlsplit(res.body["url"])
    assert f"{url.scheme}://{url.netloc}" == "http://memory.invalid"  # the store origin
    assert url.path == f"/{key}"
    assert "Résumé" not in res.body["url"]  # the filename is not part of the object key
    query = parse_qs(url.query)
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9%20final.pdf" in query["rcd"][0]
    assert query["rcd"][0].startswith("inline;")
    expires = datetime.fromisoformat(res.body["expires_at"])
    assert before + timedelta(seconds=PRESIGN_TTL - 5) <= expires
    assert expires <= datetime.now(UTC) + timedelta(seconds=PRESIGN_TTL + 5)
    assert abs(int(query["exp"][0]) - expires.timestamp()) <= 5
    served = store.simulate_get(res.body["url"])
    assert served.status == 200
    assert served.body == b"content of " + key.encode()
    # The URL is for that version only: tampering is rejected by the store.
    assert store.simulate_get(res.body["url"].replace("original", "orig1nal")).status == 403


def test_content_url_validation_and_access(world: World, store: InMemoryObjectStore) -> None:
    doc = world.seed(title="Acc")
    put(store, world.org_id, doc.id, doc.versions[0])
    path = world.doc_path(doc.id, f"/versions/{doc.versions[0]}/content-url")
    for bad in ("download", "INLINE", ""):
        res = world.api.get(path, world.t.owner, params={"disposition": bad})
        assert (res.status, res.body["detail"]) == (422, "invalid_request"), bad
    res = world.api.get(path, world.plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    res = world.api.get(path, world.t.outsider)
    assert_denied(world.api, res, 404, action="document.read", reason="not_org_member")
    assert world.api.get(path, None).status == 401
    world.grant(doc.id, world.plain, "read")
    assert world.api.get(path, world.plain).status == 200


def test_storage_unavailable_is_503(world: World, client: TestClient) -> None:
    doc = world.seed(title="Down")

    class Down(InMemoryObjectStore):
        def presign_get(self, *args: Any, **kwargs: Any) -> str:
            raise StorageUnavailable("object store is down")

    app: Any = client.app
    previous = app.dependency_overrides[get_object_store]
    app.dependency_overrides[get_object_store] = lambda: Down()
    try:
        res = world.api.get(
            world.doc_path(doc.id, f"/versions/{doc.versions[0]}/content-url"), world.t.owner
        )
    finally:
        app.dependency_overrides[get_object_store] = previous
    assert (res.status, res.body) == (503, {"detail": "storage_unavailable"})


def test_purged_version_has_no_content_url(world: World) -> None:
    doc = world.seed(title="Purged", versions=2)
    with world.engine.begin() as conn:
        conn.execute(
            text("UPDATE document_versions SET purged_at = now() WHERE id = :v"),
            {"v": doc.versions[0]},
        )
    path = world.doc_path(doc.id, f"/versions/{doc.versions[0]}/content-url")
    assert world.api.get(path, world.t.owner).status == 404
    ok = world.doc_path(doc.id, f"/versions/{doc.versions[1]}/content-url")
    assert world.api.get(ok, world.t.owner).status == 200


# --- versions cannot be silently overwritten ------------------------------------------------


def test_no_endpoint_mutates_a_version(world: World, client: TestClient) -> None:
    doc = world.seed(title="Frozen", versions=2)
    version = doc.versions[0]
    before = world.row("SELECT * FROM document_versions WHERE id = :v", v=version)
    base = world.doc_path(doc.id, "/versions")
    for suffix in ("", f"/{version}", f"/{version}/content-url"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            if suffix == "" and method == "POST":
                continue  # asserted below with the rest
            res = world.api.call(
                method, base + suffix, world.t.owner, json={"original_filename": "x"}
            )
            assert res.status in (404, 405), (method, suffix, res.status)
            assert res.status == 405, (method, suffix)
    res = world.api.call("POST", base, world.t.owner, json={})
    assert res.status == 405
    assert world.row("SELECT * FROM document_versions WHERE id = :v", v=version) == before
    # PATCH on the document only accepts the title: version data cannot be smuggled in.
    res = world.api.patch(
        world.doc_path(doc.id), world.t.owner, {"title": "ok", "latest_version_id": str(version)}
    )
    assert res.status == 422
    assert (
        world.row(
            "SELECT latest_version_id FROM documents WHERE id = :d", d=doc.id
        ).latest_version_id
        == doc.versions[1]
    )


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("original_filename", "overwritten.pdf"),
        ("content_type", "text/html"),
        ("size_bytes", 999999),
        ("sha256", "f" * 64),
        ("object_key", "org/x/y/z/original"),
        ("version_number", 42),
        ("created_at", datetime(2001, 1, 1, tzinfo=UTC)),
    ],
)
def test_the_app_role_cannot_update_a_version(
    world: World, app_engine: Engine, attribute: str, value: object
) -> None:
    doc = world.seed(title="Immutable")
    version_id = doc.versions[0]
    before = world.row("SELECT * FROM document_versions WHERE id = :v", v=version_id)
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=world.t.owner.id, org_id=world.org_id, engine=app_engine) as s,
    ):
        version = s.get(DocumentVersion, version_id)
        assert version is not None
        setattr(version, attribute, value)
        s.flush()
    assert sqlstate(exc.value) == "42501", exc.value
    assert world.row("SELECT * FROM document_versions WHERE id = :v", v=version_id) == before


def test_the_app_role_cannot_delete_a_version(world: World, app_engine: Engine) -> None:
    doc = world.seed(title="Immutable")
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=world.t.owner.id, org_id=world.org_id, engine=app_engine) as s,
    ):
        version = s.get(DocumentVersion, doc.versions[0])
        assert version is not None
        s.delete(version)
        s.flush()
    assert sqlstate(exc.value) == "42501", exc.value
    assert world.row("SELECT 1 FROM document_versions WHERE id = :v", v=doc.versions[0]) is not None


def test_a_version_number_cannot_be_reused(world: World) -> None:
    doc = world.seed(title="Numbered", versions=2)
    with pytest.raises(IntegrityError):
        add_version(world, doc.id, 2, "again.pdf")
    versions = world.api.get(world.doc_path(doc.id, "/versions"), world.t.owner).body["items"]
    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[0]["original_filename"] == "file.pdf"


def test_purged_at_moves_once_and_nothing_else_does(world: World, app_engine: Engine) -> None:
    doc = world.seed(title="Purge once")
    with tenant_session(user_id=world.t.owner.id, org_id=world.org_id, engine=app_engine) as s:
        version = s.get(DocumentVersion, doc.versions[0])
        assert version is not None
        version.purged_at = datetime.now(UTC)  # the purge job's one permitted change
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=world.t.owner.id, org_id=world.org_id, engine=app_engine) as s,
    ):
        version = s.get(DocumentVersion, doc.versions[0])
        assert version is not None
        version.purged_at = None
        s.flush()
    assert sqlstate(exc.value) == "42501"
