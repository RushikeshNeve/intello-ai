"""``POST {ws}/uploads/{id}/finalize`` (M02.01.04).

Acceptance: "Missing, altered or orphan objects rejected".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from tenancy_support import assert_denied, assert_success_audit
from upload_support import Scene, UploadApi, sha_hex

from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.modules.documents.schemas import sha256_hex_to_b64
from app.modules.storage.keys import original_key
from app.modules.storage.ports import ChecksumMismatch, StorageUnavailable

DATA = b"the quarterly numbers"


def _key(session: dict[str, Any], scene: Scene) -> str:
    return original_key(scene.org_id, session["document_id"], session["version_id"])


# ---------------------------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------------------------


def test_custom_title_is_used_for_a_new_document(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA, filename="scan0042.pdf", title="Q3 board pack")
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert res.status == 201, res.body
    assert res.body["document"]["title"] == "Q3 board pack"
    # The filename is still recorded on the version, separately from the title.
    assert res.body["version"]["original_filename"] == "scan0042.pdf"


def test_title_falls_back_to_the_filename_when_not_given(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA, filename="notes.v2.md")
    assert api.session_row(session["upload_id"])["title"] is None
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert res.body["document"]["title"] == "notes.v2"


def test_title_is_ignored_for_a_new_version_of_an_existing_document(
    api: UploadApi, scene: Scene
) -> None:
    first = api.finalize(
        scene, scene.editor, api.session(scene, scene.editor, DATA, title="Original")["upload_id"]
    )
    document_id = first.body["document"]["id"]
    more = b"second version"
    session = api.session(
        scene, scene.editor, more, document_id=document_id, title="A different title"
    )
    assert api.session_row(session["upload_id"])["title"] is None  # a version never renames
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert res.status == 201, res.body
    assert res.body["document"]["title"] == "Original"
    assert res.body["version"]["version_number"] == 2


def test_finalize_creates_document_version_source_and_audit(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA, filename="Q3 plan.final.txt")
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert res.status == 201, res.body
    document, version = res.body["document"], res.body["version"]

    assert document["id"] == session["document_id"]
    assert document["org_id"] == str(scene.org_id)
    assert document["workspace_id"] == str(scene.ws)
    assert document["title"] == "Q3 plan.final"  # filename without its extension
    assert document["created_by"] == str(scene.editor.id)
    assert document["version_count"] == 1
    assert document["latest_version"] == version
    assert document["permissions"] == {"write": True, "manage": False}
    assert set(document) == {
        "id", "org_id", "workspace_id", "title", "created_by", "created_at", "updated_at",
        "version_count", "latest_version", "permissions",
    }  # fmt: skip
    assert version == {
        "id": session["version_id"],
        "document_id": session["document_id"],
        "version_number": 1,
        "original_filename": "Q3 plan.final.txt",
        "content_type": "text/plain",
        "size_bytes": len(DATA),
        "sha256": sha_hex(DATA),
        "created_by": str(scene.editor.id),
        "created_at": version["created_at"],
        "retention": {"status": "current", "purge_after": None},
    }
    datetime.fromisoformat(version["created_at"])

    # Database
    rows = api.versions(session["document_id"])
    assert len(rows) == 1
    assert rows[0]["object_key"] == _key(session, scene)
    doc_row = api.rows("SELECT * FROM documents WHERE id = :d", d=session["document_id"])[0]
    assert str(doc_row["latest_version_id"]) == session["version_id"]
    assert doc_row["deleted_at"] is None
    sources = api.rows("SELECT * FROM sources WHERE workspace_id = :w", w=scene.ws)
    assert [(s["kind"], s["name"]) for s in sources] == [("upload", "Uploads")]
    assert doc_row["source_id"] == sources[0]["id"]
    srow = api.session_row(session["upload_id"])
    assert srow["state"] == "finalized"
    assert srow["finalized_at"] is not None
    assert srow["failure_code"] is None

    # Audit: one event, actor = principal, ids/size/type only (never the filename or the key).
    meta = assert_success_audit(
        api, res, "document.created", actor=scene.editor, org_id=scene.org_id
    )
    assert meta == {
        "workspace_id": str(scene.ws),
        "document_id": session["document_id"],
        "version_id": session["version_id"],
        "version_number": 1,
        "upload_id": session["upload_id"],
        "size_bytes": len(DATA),
        "content_type": "text/plain",
    }
    event = api.rows(
        "SELECT target_type, target_id FROM audit_events WHERE request_id = :r", r=res.request_id
    )
    assert event == [{"target_type": "document", "target_id": session["document_id"]}]


def test_owner_sees_manage_permission(api: UploadApi, scene: Scene) -> None:
    _, res = api.uploaded(scene, scene.owner, DATA)
    assert res.body["document"]["permissions"] == {"write": True, "manage": True}


def test_source_is_created_once_per_workspace(api: UploadApi, scene: Scene) -> None:
    for i in range(3):
        api.uploaded(scene, scene.editor, f"doc {i}".encode())
    assert api.count("sources", "workspace_id = :w AND kind = 'upload'", w=scene.ws) == 1
    api.uploaded(scene, scene.owner, DATA)  # another uploader, same workspace
    assert api.count("sources", "workspace_id = :w", w=scene.ws) == 1


def test_versions_are_numbered_consecutively(api: UploadApi, scene: Scene) -> None:
    first, res1 = api.uploaded(scene, scene.editor, b"v1 bytes")
    doc = res1.body["document"]["id"]
    assert res1.body["version"]["version_number"] == 1

    s2 = api.session(scene, scene.editor, b"v2 bytes!", document_id=doc, filename="second.txt")
    res2 = api.finalize(scene, scene.editor, s2["upload_id"])
    assert res2.status == 201, res2.body
    assert res2.body["version"]["version_number"] == 2
    assert res2.body["document"]["id"] == doc
    assert res2.body["document"]["version_count"] == 2
    assert res2.body["document"]["latest_version"]["id"] == s2["version_id"]
    assert res2.body["document"]["title"] == "report"  # an upload never renames the document
    assert res2.body["version"]["retention"]["status"] == "current"
    assert_success_audit(
        api, res2, "document.version.created", actor=scene.editor, org_id=scene.org_id
    )

    s3 = api.session(scene, scene.ws_admin, b"v3", document_id=doc)
    res3 = api.finalize(scene, scene.ws_admin, s3["upload_id"])
    assert res3.body["version"]["version_number"] == 3
    assert res3.body["version"]["created_by"] == str(scene.ws_admin.id)
    assert res3.body["document"]["version_count"] == 3

    numbers = [v["version_number"] for v in api.versions(doc)]
    assert numbers == [1, 2, 3]
    doc_row = api.rows(
        "SELECT latest_version_id, updated_at, created_at FROM documents WHERE id = :d", d=doc
    )[0]
    assert str(doc_row["latest_version_id"]) == s3["version_id"]
    assert doc_row["updated_at"] > doc_row["created_at"]
    assert str(first["document_id"]) == doc
    # Only one document for the three sessions
    assert api.count("documents", "workspace_id = :w", w=scene.ws) == 1


def test_repeat_finalize_of_an_older_version_reports_it_superseded(
    api: UploadApi, scene: Scene
) -> None:
    s1, res1 = api.uploaded(scene, scene.editor, b"one")
    doc = res1.body["document"]["id"]
    s2 = api.session(scene, scene.editor, b"two!", document_id=doc)
    assert api.finalize(scene, scene.editor, s2["upload_id"]).status == 201
    again = api.finalize(scene, scene.editor, s1["upload_id"])
    assert again.status == 200
    assert again.body["version"]["version_number"] == 1
    assert again.body["version"]["retention"]["status"] == "superseded"
    assert again.body["document"]["latest_version"]["id"] == s2["version_id"]
    assert again.body["document"]["version_count"] == 2


def test_member_with_write_grant_can_finalize_a_new_version(api: UploadApi, scene: Scene) -> None:
    _, res = api.uploaded(scene, scene.editor, b"first")
    doc = uuid.UUID(res.body["document"]["id"])
    api.grant(scene, doc, scene.member, "write")
    session = api.session(scene, scene.member, b"by grant", document_id=str(doc))
    out = api.finalize(scene, scene.member, session["upload_id"])
    assert out.status == 201, out.body
    assert out.body["version"]["version_number"] == 2
    assert out.body["document"]["permissions"] == {"write": True, "manage": False}


# ---------------------------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------------------------


def test_repeat_finalize_is_idempotent(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA)
    first = api.finalize(scene, scene.editor, session["upload_id"])
    second = api.finalize(scene, scene.editor, session["upload_id"])
    third = api.finalize(scene, scene.editor, session["upload_id"])
    assert (first.status, second.status, third.status) == (201, 200, 200)
    assert second.body == first.body == third.body
    assert api.count("documents", "id = :d", d=session["document_id"]) == 1
    assert len(api.versions(session["document_id"])) == 1
    events = api.count(
        "audit_events",
        "target_id = :d AND event_type = 'document.created'",
        d=session["document_id"],
    )
    assert events == 1
    assert api.audit(second.request_id) == []  # the repeat writes nothing


def test_repeat_survives_session_expiry_and_object_deletion(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore
) -> None:
    session = api.session(scene, scene.editor, DATA)
    first = api.finalize(scene, scene.editor, session["upload_id"])
    api.expire_session(session["upload_id"])
    store.delete_object(_key(session, scene))
    again = api.finalize(scene, scene.editor, session["upload_id"])
    assert again.status == 200
    assert again.body == first.body


# ---------------------------------------------------------------------------------------------
# Missing, altered and orphan objects
# ---------------------------------------------------------------------------------------------


def test_missing_object_is_409_and_session_stays_pending_until_the_put_arrives(
    api: UploadApi, scene: Scene
) -> None:
    session = api.session(scene, scene.editor, DATA, put=False)
    for _ in range(2):
        res = api.finalize(scene, scene.editor, session["upload_id"])
        assert (res.status, res.body) == (409, {"detail": "object_missing"})
    row = api.session_row(session["upload_id"])
    assert row["state"] == "pending"
    assert row["failure_code"] is None
    assert api.count("documents", "id = :d", d=session["document_id"]) == 0
    assert api.versions(session["document_id"]) == []
    assert api.audit(res.request_id) == []

    api.put_bytes(session, DATA)  # the client retries its PUT
    ok = api.finalize(scene, scene.editor, session["upload_id"])
    assert ok.status == 201, ok.body


def test_altered_content_of_the_same_size_is_checksum_mismatch(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore
) -> None:
    session = api.session(scene, scene.editor, DATA, put=False)
    tampered = b"X" * len(DATA)
    key = _key(session, scene)
    store.put_object(
        key, tampered, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(tampered))
    )
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert (res.status, res.body) == (409, {"detail": "checksum_mismatch"})
    _assert_failed(api, scene, session, store, key, "checksum_mismatch")


def test_altered_content_of_another_size_is_size_mismatch(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore
) -> None:
    session = api.session(scene, scene.editor, DATA, put=False)
    other = DATA + b" and more"
    key = _key(session, scene)
    store.put_object(
        key, other, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(other))
    )
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert (res.status, res.body) == (409, {"detail": "size_mismatch"})
    _assert_failed(api, scene, session, store, key, "size_mismatch")


def _assert_failed(
    api: UploadApi,
    scene: Scene,
    session: dict[str, Any],
    store: InMemoryObjectStore,
    key: str,
    code: str,
) -> None:
    assert store.head_object(key) is None  # the object was deleted
    row = api.session_row(session["upload_id"])
    assert row["state"] == "failed"
    assert row["failure_code"] == code
    assert row["finalized_at"] is None
    assert api.count("documents", "id = :d", d=session["document_id"]) == 0
    assert api.versions(session["document_id"]) == []
    # The failure is final: repeating the call repeats the error and a fresh PUT can't revive it.
    again = api.finalize(scene, scene.editor, session["upload_id"])
    assert (again.status, again.body) == (409, {"detail": code})


def test_failed_session_cannot_be_revived_and_a_new_session_works(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore
) -> None:
    session = api.session(scene, scene.editor, DATA, put=False)
    key = _key(session, scene)
    wrong = b"Y" * len(DATA)
    store.put_object(
        key, wrong, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(wrong))
    )
    assert api.finalize(scene, scene.editor, session["upload_id"]).status == 409
    # The right bytes now arrive under the same key (e.g. a late retry): still rejected.
    store.put_object(
        key, DATA, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(DATA))
    )
    again = api.finalize(scene, scene.editor, session["upload_id"])
    assert (again.status, again.body) == (409, {"detail": "checksum_mismatch"})
    assert api.versions(session["document_id"]) == []
    # A new session works.
    fresh, res = api.uploaded(scene, scene.editor, DATA)
    assert fresh["upload_id"] != session["upload_id"]
    assert res.status == 201


def test_the_browser_cannot_put_other_bytes_in_the_first_place(
    api: UploadApi, scene: Scene
) -> None:
    session = api.session(scene, scene.editor, DATA, put=False)
    with pytest.raises(ChecksumMismatch):
        api.put_bytes(session, b"Z" * len(DATA))
    assert api.finalize(scene, scene.editor, session["upload_id"]).status == 409  # object_missing


def test_orphan_objects_are_never_adopted(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore
) -> None:
    """An object at a plausible key with no session behind it cannot become a version."""
    document_id, version_id = uuid.uuid4(), uuid.uuid4()
    key = original_key(scene.org_id, document_id, version_id)
    store.put_object(
        key, DATA, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(DATA))
    )
    # Not reachable through any finalize call: ids of the document/version are not session ids.
    for guess in (document_id, version_id, uuid.uuid4()):
        res = api.finalize(scene, scene.editor, guess)
        assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert api.count("document_versions", "object_key = :k", k=key) == 0
    assert api.count("documents", "id = :d", d=document_id) == 0
    # A session for a different version never picks up this object either.
    session = api.session(scene, scene.editor, DATA, put=False)
    assert api.finalize(scene, scene.editor, session["upload_id"]).body == {
        "detail": "object_missing"
    }


def test_object_of_another_session_is_not_borrowed(api: UploadApi, scene: Scene) -> None:
    a = api.session(scene, scene.editor, DATA)  # uploaded, not finalized
    b = api.session(scene, scene.editor, DATA, put=False)  # same bytes, no object of its own
    res = api.finalize(scene, scene.editor, b["upload_id"])
    assert (res.status, res.body) == (409, {"detail": "object_missing"})
    assert api.finalize(scene, scene.editor, a["upload_id"]).status == 201


# ---------------------------------------------------------------------------------------------
# Expiry, deletion, storage outage
# ---------------------------------------------------------------------------------------------


def test_expired_session_is_410_and_marked_expired(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA)
    api.expire_session(session["upload_id"])
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert (res.status, res.body) == (410, {"detail": "upload_expired"})
    row = api.session_row(session["upload_id"])
    assert row["state"] == "expired"
    assert row["finalized_at"] is None
    assert row["failure_code"] is None
    again = api.finalize(scene, scene.editor, session["upload_id"])
    assert (again.status, again.body) == (410, {"detail": "upload_expired"})
    assert api.versions(session["document_id"]) == []


def test_document_deleted_meanwhile_is_404(api: UploadApi, scene: Scene) -> None:
    _, res = api.uploaded(scene, scene.editor, b"first")
    doc = res.body["document"]["id"]
    session = api.session(scene, scene.editor, b"second", document_id=doc)
    api.execute(
        "UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d",
        u=scene.owner.id,
        d=doc,
    )
    out = api.finalize(scene, scene.editor, session["upload_id"])
    assert (out.status, out.body) == (404, {"detail": "not_found"})
    assert len(api.versions(doc)) == 1
    assert api.session_row(session["upload_id"])["state"] == "pending"
    # Repeating the finalize of the already finalized first session is also a 404 now.
    first_session = api.rows(
        "SELECT id FROM upload_sessions WHERE state = 'finalized' AND document_id = :d", d=doc
    )[0]
    assert api.finalize(scene, scene.editor, first_session["id"]).status == 404


def test_storage_unavailable_keeps_the_session_pending(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = api.session(scene, scene.editor, DATA)

    def boom(*_a: Any, **_k: Any) -> None:
        raise StorageUnavailable("down")

    monkeypatch.setattr(store, "head_object", boom)
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert (res.status, res.body) == (503, {"detail": "storage_unavailable"})
    assert api.session_row(session["upload_id"])["state"] == "pending"
    monkeypatch.undo()
    assert api.finalize(scene, scene.editor, session["upload_id"]).status == 201


def test_failed_delete_does_not_mark_the_session_failed(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = api.session(scene, scene.editor, DATA, put=False)
    wrong = b"W" * len(DATA)
    key = _key(session, scene)
    store.put_object(
        key, wrong, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(wrong))
    )

    def boom(*_a: Any, **_k: Any) -> None:
        raise StorageUnavailable("down")

    monkeypatch.setattr(store, "delete_object", boom)
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert (res.status, res.body) == (503, {"detail": "storage_unavailable"})
    assert (
        api.session_row(session["upload_id"])["state"] == "pending"
    )  # never 'failed' + object left
    monkeypatch.undo()
    assert api.finalize(scene, scene.editor, session["upload_id"]).body == {
        "detail": "checksum_mismatch"
    }
    assert store.head_object(key) is None


# ---------------------------------------------------------------------------------------------
# Who may finalize
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("other", ["owner", "org_admin", "ws_admin"])
def test_only_the_creator_may_finalize(api: UploadApi, scene: Scene, other: str) -> None:
    session = api.session(scene, scene.editor, DATA)
    res = api.finalize(scene, getattr(scene, other), session["upload_id"])
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    rows = api.audit(res.request_id)
    assert [(r["event_type"], r["outcome"]) for r in rows] == [("authz.denied", "denied")]
    assert rows[0]["metadata"] == {"action": "document.write", "reason": "not_upload_creator"}
    assert api.session_row(session["upload_id"])["state"] == "pending"
    assert (
        api.finalize(scene, scene.editor, session["upload_id"]).status == 201
    )  # the creator still can


def test_outsider_and_unauthenticated(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA)
    res = api.finalize(scene, scene.outsider, session["upload_id"])
    assert_denied(api, res, 404, action="document.write", reason="not_org_member")
    assert api.finalize(scene, None, session["upload_id"]).status == 401
    assert api.session_row(session["upload_id"])["state"] == "pending"


def test_unknown_and_malformed_session_ids(api: UploadApi, scene: Scene) -> None:
    res = api.finalize(scene, scene.editor, uuid.uuid4())
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert api.audit(res.request_id) == []
    assert api.finalize(scene, scene.editor, "not-a-uuid").status == 422


def test_session_of_another_workspace_or_org_is_404(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.owner, DATA)
    wrong_ws = api.finalize(scene, scene.owner, session["upload_id"], ws=scene.ws2)
    assert (wrong_ws.status, wrong_ws.body) == (404, {"detail": "not_found"})
    foreign = api.finalize(
        scene, scene.owner, session["upload_id"], ws=scene.other_ws, org_id=scene.other_org_id
    )
    assert foreign.status == 404
    # Even the owner of the other org (who is not a member here) gets nothing.
    theirs = api.finalize(
        scene, scene.outsider, session["upload_id"], ws=scene.other_ws, org_id=scene.other_org_id
    )
    assert theirs.status == 404
    assert api.session_row(session["upload_id"])["state"] == "pending"


def test_authorization_is_rechecked_at_finalize(api: UploadApi, scene: Scene) -> None:
    # Demoted to viewer after creating the session -> 403.
    s1 = api.session(scene, scene.editor, b"one")
    res = api.put(
        f"/v1/orgs/{scene.org_id}/workspaces/{scene.ws}/members/{scene.editor.id}",
        scene.owner,
        {"role": "viewer"},
    )
    assert res.status == 200
    out = api.finalize(scene, scene.editor, s1["upload_id"])
    assert_denied(api, out, 403, action="document.write", reason="insufficient_role")
    assert api.session_row(s1["upload_id"])["state"] == "pending"
    assert api.versions(s1["document_id"]) == []
    # Removed from the workspace -> 404.
    res = api.delete(
        f"/v1/orgs/{scene.org_id}/workspaces/{scene.ws}/members/{scene.editor.id}", scene.owner
    )
    assert res.status == 204
    out = api.finalize(scene, scene.editor, s1["upload_id"])
    assert_denied(api, out, 404, action="document.write", reason="not_workspace_member")


def test_revoked_grant_blocks_finalize_of_a_version_session(api: UploadApi, scene: Scene) -> None:
    _, res = api.uploaded(scene, scene.editor, b"first")
    doc = uuid.UUID(res.body["document"]["id"])
    api.grant(scene, doc, scene.member, "write")
    session = api.session(scene, scene.member, b"second", document_id=str(doc))
    api.execute(
        "UPDATE resource_grants SET revoked_at = now(), revoked_by = :u WHERE resource_id = :d",
        u=scene.owner.id,
        d=doc,
    )
    out = api.finalize(scene, scene.member, session["upload_id"])
    assert_denied(api, out, 404, action="document.write", reason="not_workspace_member")
    assert len(api.versions(doc)) == 1


def test_finalize_of_a_valid_upload_needs_no_body(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA)
    res = api.post(
        f"/v1/orgs/{scene.org_id}/workspaces/{scene.ws}/uploads/{session['upload_id']}/finalize",
        scene.editor,
        None,
    )
    assert res.status == 201
