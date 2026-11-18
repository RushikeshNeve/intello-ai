"""``POST {ws}/uploads`` (M02.01.03): authorization, validation, quotas, session shape.

Acceptance: "Unauthorized/oversize uploads rejected".
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from tenancy_support import assert_denied
from upload_support import Scene, UploadApi, body_for, sha_hex

from app.infrastructure.object_storage.memory import BASE_URL, InMemoryObjectStore
from app.modules.documents.schemas import sha256_hex_to_b64
from app.modules.storage.keys import original_key
from app.modules.storage.ports import StorageUnavailable

DATA = b"hello upload"
Limits = Callable[..., None]

# ---------------------------------------------------------------------------------------------
# Authorization: new document
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "org_admin", "ws_admin", "editor"])
def test_writers_may_create_a_session(api: UploadApi, scene: Scene, role: str) -> None:
    res = api.create(scene, getattr(scene, role), body_for(DATA))
    assert res.status == 201, res.body
    assert api.audit(res.request_id) == []  # no event type exists for session creation


def test_viewer_is_forbidden_and_audited(api: UploadApi, scene: Scene) -> None:
    res = api.create(scene, scene.viewer, body_for(DATA))
    assert_denied(api, res, 403, action="document.write", reason="insufficient_role")
    assert api.count("upload_sessions", "org_id = :o", o=scene.org_id) == 0


def test_org_member_without_workspace_role_gets_404(api: UploadApi, scene: Scene) -> None:
    res = api.create(scene, scene.member, body_for(DATA))
    assert_denied(api, res, 404, action="document.write", reason="not_workspace_member")


def test_non_member_of_the_org_gets_404(api: UploadApi, scene: Scene) -> None:
    res = api.create(scene, scene.outsider, body_for(DATA))
    assert_denied(api, res, 404, action="document.write", reason="not_org_member")


def test_unauthenticated_is_401(api: UploadApi, scene: Scene) -> None:
    res = api.create(scene, None, body_for(DATA))
    assert res.status == 401


def test_workspace_of_another_org_is_404(api: UploadApi, scene: Scene) -> None:
    """The owner of org A uses a workspace id of org B under org A's path: 404, nothing stored."""
    res = api.create(scene, scene.owner, body_for(DATA), ws=scene.other_ws)
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert api.count("upload_sessions", "org_id = :o", o=scene.org_id) == 0


def test_other_orgs_path_is_404_for_everyone(api: UploadApi, scene: Scene) -> None:
    path = f"/v1/orgs/{scene.other_org_id}/workspaces/{scene.other_ws}/uploads"
    res = api.post(path, scene.owner, body_for(DATA))
    assert_denied(api, res, 404, action="document.write", reason="not_org_member")
    res = api.post(
        f"/v1/orgs/{uuid.uuid4()}/workspaces/{scene.ws}/uploads", scene.owner, body_for(DATA)
    )
    assert (res.status, res.body) == (404, {"detail": "not_found"})


def test_unauthorized_callers_learn_nothing_about_quotas_or_validation(
    api: UploadApi, scene: Scene, limits: Limits
) -> None:
    limits(UPLOAD_MAX_BYTES=5, UPLOAD_MAX_PENDING_PER_USER=1, ORG_STORAGE_QUOTA_BYTES=10)
    api.session(scene, scene.editor, b"abc", put=False)  # fills the editor's own cap
    oversize = body_for(b"x" * 50)
    assert_denied(
        api,
        api.create(scene, scene.viewer, oversize),
        403,
        action="document.write",
        reason="insufficient_role",
    )
    assert_denied(
        api,
        api.create(scene, scene.member, oversize),
        404,
        action="document.write",
        reason="not_workspace_member",
    )
    assert_denied(
        api,
        api.create(scene, scene.outsider, oversize),
        404,
        action="document.write",
        reason="not_org_member",
    )
    # ...and neither does the shape of their (invalid) body matter.
    res = api.create(scene, scene.viewer, {"filename": 1})
    assert (res.status, res.body) == (403, {"detail": "forbidden"})
    res = api.create(scene, scene.outsider, {"nonsense": True})
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    # An authorized caller sees the real answers.
    assert api.create(scene, scene.editor, oversize).body == {"detail": "file_too_large"}


# ---------------------------------------------------------------------------------------------
# Authorization: new version of an existing document (workspace role + document grants)
# ---------------------------------------------------------------------------------------------


def _existing_document(api: UploadApi, scene: Scene) -> uuid.UUID:
    _, res = api.uploaded(scene, scene.editor, b"first version")
    return uuid.UUID(res.body["document"]["id"])


def test_new_version_by_workspace_writers(api: UploadApi, scene: Scene) -> None:
    doc = _existing_document(api, scene)
    for user in (scene.editor, scene.ws_admin, scene.owner):
        res = api.create(scene, user, body_for(DATA, document_id=str(doc)))
        assert res.status == 201, res.body
        assert res.body["document_id"] == str(doc)


def test_new_version_denied_for_viewer_and_unrelated_member(api: UploadApi, scene: Scene) -> None:
    doc = str(_existing_document(api, scene))
    res = api.create(scene, scene.viewer, body_for(DATA, document_id=doc))
    assert_denied(api, res, 403, action="document.write", reason="insufficient_role")
    res = api.create(scene, scene.member, body_for(DATA, document_id=doc))
    assert_denied(api, res, 404, action="document.write", reason="not_workspace_member")
    res = api.create(scene, scene.outsider, body_for(DATA, document_id=doc))
    assert_denied(api, res, 404, action="document.write", reason="not_org_member")


@pytest.mark.parametrize("permission", ["write", "manage"])
def test_member_with_write_or_manage_grant_may_upload_a_version(
    api: UploadApi, scene: Scene, permission: str
) -> None:
    doc = _existing_document(api, scene)
    api.grant(scene, doc, scene.member, permission)
    res = api.create(scene, scene.member, body_for(DATA, document_id=str(doc)))
    assert res.status == 201, res.body
    # ...but only for that document, and not a new one.
    assert api.create(scene, scene.member, body_for(DATA)).status == 404


def test_member_with_read_grant_is_forbidden_not_hidden(api: UploadApi, scene: Scene) -> None:
    doc = _existing_document(api, scene)
    api.grant(scene, doc, scene.member, "read")
    res = api.create(scene, scene.member, body_for(DATA, document_id=str(doc)))
    assert_denied(api, res, 403, action="document.write", reason="insufficient_role")


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_revoked_or_expired_grant_does_not_authorize(
    api: UploadApi, scene: Scene, state: str
) -> None:
    doc = _existing_document(api, scene)
    api.grant(
        scene, doc, scene.member, "write", revoked=state == "revoked", expired=state == "expired"
    )
    res = api.create(scene, scene.member, body_for(DATA, document_id=str(doc)))
    assert_denied(api, res, 404, action="document.write", reason="not_workspace_member")


def test_grant_on_another_document_does_not_help(api: UploadApi, scene: Scene) -> None:
    doc = _existing_document(api, scene)
    other = _existing_document(api, scene)
    api.grant(scene, other, scene.member, "write")
    res = api.create(scene, scene.member, body_for(DATA, document_id=str(doc)))
    assert res.status == 404


def test_unknown_deleted_and_foreign_documents_are_404(api: UploadApi, scene: Scene) -> None:
    doc = _existing_document(api, scene)
    unknown = api.create(scene, scene.editor, body_for(DATA, document_id=str(uuid.uuid4())))
    assert (unknown.status, unknown.body) == (404, {"detail": "not_found"})
    # The document lives in ws; addressing it through ws2 finds nothing.
    other_ws = api.create(scene, scene.owner, body_for(DATA, document_id=str(doc)), ws=scene.ws2)
    assert other_ws.status == 404
    # A viewer cannot tell an unknown id from a real one (both 403).
    viewer = api.create(scene, scene.viewer, body_for(DATA, document_id=str(uuid.uuid4())))
    assert viewer.status == 403
    api.execute(
        "UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d",
        u=scene.owner.id,
        d=doc,
    )
    for user in (scene.editor, scene.owner):
        res = api.create(scene, user, body_for(DATA, document_id=str(doc)))
        assert (res.status, res.body) == (404, {"detail": "not_found"})


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------


def _invalid(api: UploadApi, scene: Scene, body: Any, field: str | None = None) -> None:
    res = api.create(scene, scene.editor, body)
    assert res.status == 422, res.body
    assert res.body["detail"] == "invalid_request"
    if field is not None:
        assert any(field in err["loc"] for err in res.body["errors"]), res.body
    assert api.count("upload_sessions", "org_id = :o", o=scene.org_id) == 0


@pytest.mark.parametrize(
    "sha",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63, sha_hex(b"x").upper()],
)
def test_bad_sha256_is_422(api: UploadApi, scene: Scene, sha: str) -> None:
    _invalid(api, scene, {**body_for(DATA), "sha256": sha}, "sha256")


@pytest.mark.parametrize(
    "content_type",
    [
        "",
        "text",
        "text/",
        "/plain",
        "text/plain; charset=utf-8",
        "a b/c",
        "text/pl ain",
        "x" * 300 + "/y",
    ],
)
def test_bad_content_type_is_422(api: UploadApi, scene: Scene, content_type: str) -> None:
    _invalid(api, scene, {**body_for(DATA), "content_type": content_type}, "content_type")


@pytest.mark.parametrize("size", [0, -1])
def test_size_must_be_positive(api: UploadApi, scene: Scene, size: int) -> None:
    _invalid(api, scene, {**body_for(DATA), "size_bytes": size}, "size_bytes")


@pytest.mark.parametrize("filename", ["", "   ", "/", "..", "a/", "\x00\x01", "x" * 256])
def test_unusable_filenames_are_422(api: UploadApi, scene: Scene, filename: str) -> None:
    _invalid(api, scene, {**body_for(DATA), "filename": filename}, "filename")


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"filename": "a.txt"},
        {**body_for(DATA), "unexpected": 1},
        {**body_for(DATA), "document_id": "not-a-uuid"},
        {**body_for(DATA), "title": "   "},
        {**body_for(DATA), "title": "t" * 256},
    ],
)
def test_malformed_bodies_are_422(api: UploadApi, scene: Scene, body: Any) -> None:
    _invalid(api, scene, body)


def test_filename_is_sanitized_and_never_part_of_the_key(api: UploadApi, scene: Scene) -> None:
    raw = "C:\\fakepath\\..\\my  report\t\u202efdp.exe\x07 "
    res = api.create(scene, scene.editor, body_for(DATA, filename=raw))
    assert res.status == 201, res.body
    row = api.session_row(res.body["upload_id"])
    assert row["filename"] == "my report fdp.exe"
    assert "report" not in row["object_key"]
    assert "report" not in res.body["upload"]["url"].split("?")[0]
    traversal = api.create(scene, scene.editor, body_for(DATA, filename="../../etc/passwd"))
    assert api.session_row(traversal.body["upload_id"])["filename"] == "passwd"


def test_content_type_is_normalized_to_lower_case(api: UploadApi, scene: Scene) -> None:
    res = api.create(scene, scene.editor, body_for(DATA, content_type="Text/Plain"))
    assert res.status == 201
    assert res.body["upload"]["headers"]["Content-Type"] == "text/plain"


# ---------------------------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------------------------


def test_file_too_large_boundary(api: UploadApi, scene: Scene, limits: Limits) -> None:
    limits(UPLOAD_MAX_BYTES=10)
    assert api.create(scene, scene.editor, body_for(b"x" * 10)).status == 201  # equal: allowed
    res = api.create(scene, scene.editor, body_for(b"x" * 11))
    assert (res.status, res.body) == (413, {"detail": "file_too_large"})
    assert api.count("upload_sessions", "org_id = :o", o=scene.org_id) == 1


def test_pending_cap_is_per_user(api: UploadApi, scene: Scene, limits: Limits) -> None:
    limits(UPLOAD_MAX_PENDING_PER_USER=2)
    for _ in range(2):
        assert api.create(scene, scene.editor, body_for(DATA)).status == 201
    res = api.create(scene, scene.editor, body_for(DATA))
    assert (res.status, res.body) == (429, {"detail": "too_many_uploads"})
    assert api.create(scene, scene.owner, body_for(DATA)).status == 201  # other users unaffected


def test_expired_and_finished_sessions_free_the_cap(
    api: UploadApi, scene: Scene, limits: Limits
) -> None:
    limits(UPLOAD_MAX_PENDING_PER_USER=2)
    first = api.session(scene, scene.editor, b"one")
    second = api.session(scene, scene.editor, b"two", put=False)
    assert api.create(scene, scene.editor, body_for(DATA)).status == 429
    assert api.finalize(scene, scene.editor, first["upload_id"]).status == 201  # finalized: free
    assert api.create(scene, scene.editor, body_for(DATA)).status == 201
    assert api.create(scene, scene.editor, body_for(DATA)).status == 429
    api.expire_session(second["upload_id"])  # lapsed while still 'pending': free
    assert api.create(scene, scene.editor, body_for(DATA)).status == 201


def test_org_quota_counts_pending_sessions_and_boundary(
    api: UploadApi, scene: Scene, limits: Limits
) -> None:
    limits(ORG_STORAGE_QUOTA_BYTES=100)
    assert api.create(scene, scene.editor, body_for(b"a" * 60)).status == 201
    over = api.create(scene, scene.owner, body_for(b"b" * 41))  # another user, same org: 101 > 100
    assert (over.status, over.body) == (409, {"detail": "quota_exceeded"})
    assert api.create(scene, scene.owner, body_for(b"b" * 40)).status == 201  # exactly 100
    assert api.create(scene, scene.owner, body_for(b"c")).status == 409


def test_org_quota_counts_finalized_versions_but_not_purged_or_expired(
    api: UploadApi, scene: Scene, limits: Limits
) -> None:
    limits(ORG_STORAGE_QUOTA_BYTES=100)
    _, res = api.uploaded(scene, scene.editor, b"a" * 60)
    assert api.create(scene, scene.editor, body_for(b"b" * 41)).status == 409
    version_id = res.body["version"]["id"]
    api.execute("UPDATE document_versions SET purged_at = now() WHERE id = :v", v=version_id)
    pending = api.create(scene, scene.editor, body_for(b"b" * 100))
    assert pending.status == 201  # purged bytes no longer count
    assert api.create(scene, scene.editor, body_for(b"c")).status == 409
    api.expire_session(pending.body["upload_id"])
    assert (
        api.create(scene, scene.editor, body_for(b"c" * 100)).status == 201
    )  # expired: not counted


def test_other_orgs_usage_does_not_count(api: UploadApi, scene: Scene, limits: Limits) -> None:
    limits(ORG_STORAGE_QUOTA_BYTES=100)
    other = api.scene()
    assert api.create(other, other.editor, body_for(b"a" * 100)).status == 201
    assert api.create(scene, scene.editor, body_for(b"a" * 100)).status == 201


def test_quota_check_order_is_size_then_pending_then_quota(
    api: UploadApi, scene: Scene, limits: Limits
) -> None:
    limits(UPLOAD_MAX_BYTES=50, UPLOAD_MAX_PENDING_PER_USER=1, ORG_STORAGE_QUOTA_BYTES=60)
    assert api.create(scene, scene.editor, body_for(b"a" * 50)).status == 201
    # too large AND over the pending cap AND over the quota -> 413
    assert api.create(scene, scene.editor, body_for(b"a" * 51)).status == 413
    # within the size limit but over the cap AND over the quota (50 + 20 > 60) -> 429
    assert api.create(scene, scene.editor, body_for(b"a" * 20)).status == 429
    # another user: cap not reached, quota exceeded -> 409
    assert api.create(scene, scene.owner, body_for(b"a" * 20)).status == 409


# ---------------------------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------------------------


def test_session_shape_and_database_row(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore
) -> None:
    started = time.time()
    res = api.create(
        scene, scene.editor, body_for(DATA, filename="Q3 plan.txt", title="Ignored title")
    )
    assert res.status == 201, res.body
    body = res.body
    assert set(body) == {"upload_id", "document_id", "version_id", "expires_at", "upload", "limits"}
    assert body["limits"] == {"max_bytes": 104_857_600}
    key = original_key(scene.org_id, body["document_id"], body["version_id"])
    upload = body["upload"]
    assert upload["method"] == "PUT"
    assert upload["url"].startswith(f"{BASE_URL}/{key}?")
    # Exactly what the store signed (the client must send these headers verbatim).
    assert upload["headers"] == {
        "Content-Type": "text/plain",
        "x-amz-checksum-sha256": sha256_hex_to_b64(sha_hex(DATA)),
    }
    row = api.session_row(body["upload_id"])
    assert row["state"] == "pending"
    assert row["failure_code"] is None
    assert row["finalized_at"] is None
    assert row["object_key"] == key
    assert (row["filename"], row["content_type"], row["size_bytes"], row["sha256"]) == (
        "Q3 plan.txt",
        "text/plain",
        len(DATA),
        sha_hex(DATA),
    )
    assert (row["created_by"], row["org_id"], row["workspace_id"]) == (
        scene.editor.id,
        scene.org_id,
        scene.ws,
    )
    assert str(row["document_id"]) == body["document_id"]
    assert str(row["version_id"]) == body["version_id"]
    assert datetime.fromisoformat(body["expires_at"]) == row["expires_at"]
    assert abs(row["expires_at"].timestamp() - (started + 3600)) < 30  # UPLOAD_SESSION_TTL_SECONDS
    # A new document has no row until finalize; nothing was stored yet either.
    assert api.count("documents", "id = :d", d=body["document_id"]) == 0
    assert store.head_object(key) is None


def test_session_ids_are_fresh_every_time(api: UploadApi, scene: Scene) -> None:
    a = api.create(scene, scene.editor, body_for(DATA)).body
    b = api.create(scene, scene.editor, body_for(DATA)).body
    assert len({a["upload_id"], b["upload_id"], a["version_id"], b["version_id"]}) == 4
    assert a["document_id"] != b["document_id"]


def test_new_version_session_reuses_the_document_id(api: UploadApi, scene: Scene) -> None:
    doc = _existing_document(api, scene)
    res = api.create(scene, scene.editor, body_for(DATA, document_id=str(doc)))
    assert res.body["document_id"] == str(doc)
    assert api.count("document_versions", "document_id = :d", d=doc) == 1  # not created yet


def test_presign_ttl_follows_s3_presign_ttl_seconds(
    api: UploadApi, scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("S3_PRESIGN_TTL_SECONDS", "120")
    res = api.create(scene, scene.editor, body_for(DATA))
    exp = int(res.body["upload"]["url"].split("exp=")[1].split("&")[0])
    assert abs(exp - (time.time() + 120)) < 30


def test_storage_unavailable_is_503_and_stores_nothing(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise StorageUnavailable("down")

    monkeypatch.setattr(store, "presign_put", boom)
    res = api.create(scene, scene.editor, body_for(DATA))
    assert (res.status, res.body) == (503, {"detail": "storage_unavailable"})
    assert api.count("upload_sessions", "org_id = :o", o=scene.org_id) == 0


def test_no_urls_or_signatures_are_logged(
    api: UploadApi, scene: Scene, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    session = api.session(scene, scene.editor, DATA)
    assert api.finalize(scene, scene.editor, session["upload_id"]).status == 201
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert session["upload"]["url"] not in logged
    assert "sig=" not in logged
    assert sha256_hex_to_b64(sha_hex(DATA)) not in logged
