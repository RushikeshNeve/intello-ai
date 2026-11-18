"""M02.02.02 / M02.02.04: get, rename (PATCH) and deletion request (DELETE) of one document."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from catalog_support import World
from sqlalchemy import text
from tenancy_support import assert_denied, assert_success_audit

from app.core.config import Settings
from app.infrastructure.object_storage.memory import InMemoryObjectStore

# --- GET -------------------------------------------------------------------------------------


def test_get_document_role_matrix(world: World) -> None:
    doc = world.seed(title="Spec", filename="spec.pdf", versions=2)
    expected = {
        "owner": (True, True),
        "admin": (True, True),
        "ws_admin": (True, True),
        "editor": (True, False),
        "viewer": (False, False),
    }
    for name, (write, manage) in expected.items():
        res = world.api.get(world.doc_path(doc.id), world.everyone[name])
        assert res.status == 200, (name, res.body)
        body = res.body
        assert body["id"] == str(doc.id)
        assert body["title"] == "Spec"
        assert body["version_count"] == 2
        assert body["latest_version"]["version_number"] == 2
        assert body["latest_version"]["original_filename"] == "spec.pdf"
        assert body["latest_version"]["retention"] == {"status": "current", "purge_after": None}
        assert body["permissions"] == {"write": write, "manage": manage}, name
        datetime.fromisoformat(body["created_at"])
        datetime.fromisoformat(body["updated_at"])


def test_get_denials_and_concealment(world: World) -> None:
    doc = world.seed(title="Spec")
    res = world.api.get(world.doc_path(doc.id), world.plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    res = world.api.get(world.doc_path(doc.id), world.t.outsider)
    assert_denied(world.api, res, 404, action="document.read", reason="not_org_member")
    assert world.api.get(world.doc_path(doc.id), None).status == 401
    # Unknown, malformed and foreign ids.
    res = world.api.get(world.doc_path(uuid.uuid4()), world.t.owner)
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert world.api.get(world.doc_path("not-a-uuid"), world.t.owner).status == 422
    foreign = world.seed_foreign(title="Foreign")
    assert world.api.get(world.doc_path(foreign.id), world.t.owner).status == 404
    assert world.api.get(world.doc_path(doc.id), world.foreign.owner).status == 404
    # The foreign owner asking through *their* org and workspace for our document id.
    res = world.api.get(
        f"/v1/orgs/{world.foreign.org_id}/workspaces/{world.foreign_ws}/documents/{doc.id}",
        world.foreign.owner,
    )
    assert res.status == 404


def test_get_with_grants(world: World) -> None:
    doc = world.seed(title="Shared")
    world.grant(doc.id, world.plain, "read")
    res = world.api.get(world.doc_path(doc.id), world.plain)
    assert res.status == 200
    assert res.body["permissions"] == {"write": False, "manage": False}
    other = world.seed(title="Other")
    res = world.api.get(world.doc_path(other.id), world.plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    world.grant(other.id, world.plain, "read", expires_in="-1 second")
    assert world.api.get(world.doc_path(other.id), world.plain).status == 404
    third = world.seed(title="Third")
    world.grant(third.id, world.plain, "manage", revoked=True)
    assert world.api.get(world.doc_path(third.id), world.plain).status == 404


# --- PATCH -----------------------------------------------------------------------------------


def test_rename_changes_title_updated_at_and_audits(world: World) -> None:
    doc = world.seed(title="Old", created_at="2026-01-01T00:00:00+00:00")
    previous_title = "Old"
    for name in ("owner", "admin", "ws_admin", "editor"):
        user = world.everyone[name]
        before = world.row("SELECT updated_at FROM documents WHERE id = :d", d=doc.id).updated_at
        res = world.api.patch(world.doc_path(doc.id), user, {"title": f"  New by {name}  "})
        assert res.status == 200, (name, res.body)
        assert res.body["title"] == f"New by {name}"
        assert res.body["id"] == str(doc.id)
        assert res.body["latest_version"]["id"] == str(doc.versions[-1])
        after = world.row("SELECT updated_at, title FROM documents WHERE id = :d", d=doc.id)
        assert after.title == f"New by {name}"
        assert after.updated_at > before
        assert datetime.fromisoformat(res.body["updated_at"]) == after.updated_at
        meta = assert_success_audit(
            world.api, res, "document.updated", actor=user, org_id=world.org_id
        )
        assert meta["workspace_id"] == str(world.ws)
        assert meta["to_title"] == f"New by {name}"
        assert meta["from_title"] == previous_title
        row = world.api.audit(res.request_id)[0]
        assert (row["target_type"], row["target_id"]) == ("document", str(doc.id))
        previous_title = f"New by {name}"


def test_rename_without_change_writes_nothing(world: World) -> None:
    doc = world.seed(title="Same", created_at="2026-01-01T00:00:00+00:00")
    before = world.row("SELECT updated_at FROM documents WHERE id = :d", d=doc.id).updated_at
    for sent in ("Same", "  Same  "):
        res = world.api.patch(world.doc_path(doc.id), world.t.owner, {"title": sent})
        assert res.status == 200
        assert res.body["title"] == "Same"
        assert world.api.audit(res.request_id) == []
    after = world.row("SELECT updated_at FROM documents WHERE id = :d", d=doc.id).updated_at
    assert after == before


@pytest.mark.parametrize(
    "body",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 256},
        {"title": "line\nbreak"},
        {"title": "tab\tinside"},
        {"title": None},
        {"title": 5},
        {},
        {"title": "ok", "extra": 1},
        {"updated_at": "2020-01-01T00:00:00Z"},
    ],
)
def test_rename_validation(world: World, body: dict[str, object]) -> None:
    doc = world.seed(title="Keep")
    res = world.api.patch(world.doc_path(doc.id), world.t.owner, body)
    assert res.status == 422
    assert res.body["detail"] == "invalid_request"
    assert world.row("SELECT title FROM documents WHERE id = :d", d=doc.id).title == "Keep"
    assert world.api.audit(res.request_id) == []


def test_rename_accepts_the_longest_title(world: World) -> None:
    doc = world.seed(title="Keep")
    res = world.api.patch(world.doc_path(doc.id), world.t.owner, {"title": "é" * 255})
    assert res.status == 200
    assert res.body["title"] == "é" * 255


def test_rename_role_matrix_and_grants(world: World) -> None:
    doc = world.seed(title="Doc")
    path = world.doc_path(doc.id)
    res = world.api.patch(path, world.viewer, {"title": "x"})
    assert_denied(world.api, res, 403, action="document.write", reason="insufficient_role")
    res = world.api.patch(path, world.plain, {"title": "x"})
    assert_denied(world.api, res, 404, action="document.write", reason="not_workspace_member")
    res = world.api.patch(path, world.t.outsider, {"title": "x"})
    assert_denied(world.api, res, 404, action="document.write", reason="not_org_member")
    assert world.api.patch(path, None, {"title": "x"}).status == 401
    assert world.row("SELECT title FROM documents WHERE id = :d", d=doc.id).title == "Doc"

    # A read grant shows the document but does not allow renaming it.
    world.grant(doc.id, world.plain, "read")
    res = world.api.patch(path, world.plain, {"title": "x"})
    assert_denied(world.api, res, 403, action="document.write", reason="insufficient_role")
    # An expired write grant does not authorize (and a read-only one remains).
    world.grant(doc.id, world.other_plain, "write", expires_in="-1 hour")
    res = world.api.patch(path, world.other_plain, {"title": "x"})
    assert_denied(world.api, res, 404, action="document.write", reason="not_workspace_member")
    # A write grant does.
    other = world.seed(title="Writable")
    world.grant(other.id, world.plain, "write")
    res = world.api.patch(world.doc_path(other.id), world.plain, {"title": "Renamed"})
    assert res.status == 200, res.body
    assert res.body["permissions"] == {"write": True, "manage": False}
    # So does a manage grant (implies write); a revoked one does not.
    third = world.seed(title="Managed")
    world.grant(third.id, world.other_plain, "manage")
    assert (
        world.api.patch(world.doc_path(third.id), world.other_plain, {"title": "M2"}).status == 200
    )
    fourth = world.seed(title="Revoked")
    world.grant(fourth.id, world.other_plain, "manage", revoked=True)
    assert (
        world.api.patch(world.doc_path(fourth.id), world.other_plain, {"title": "M2"}).status == 404
    )
    # A write grant to the viewer role lets a viewer rename.
    fifth = world.seed(title="Fifth")
    world.grant(fifth.id, world.viewer, "write", grantee_type="workspace_role", grantee_id="viewer")
    assert world.api.patch(world.doc_path(fifth.id), world.viewer, {"title": "V"}).status == 200


def test_rename_unknown_deleted_or_foreign_document_is_404(world: World) -> None:
    gone = world.seed(title="Gone", deleted=True)
    foreign = world.seed_foreign(title="Foreign")
    for doc_id in (uuid.uuid4(), gone.id, foreign.id):
        res = world.api.patch(world.doc_path(doc_id), world.t.owner, {"title": "x"})
        assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert world.row("SELECT title FROM documents WHERE id = :d", d=foreign.id).title == "Foreign"


# --- DELETE ----------------------------------------------------------------------------------


def test_delete_returns_202_and_denies_immediately_everywhere(
    world: World, store: InMemoryObjectStore
) -> None:
    doc = world.seed(title="Doomed", versions=2)
    keys = [f"org/{world.org_id}/doc/{doc.id}/{version_id}/original" for version_id in doc.versions]
    for key in keys:
        store.put_object(
            key,
            b"bytes of " + key.encode(),
            content_type="application/pdf",
            sha256_b64=_sha(b"bytes of " + key.encode()),
        )
    other = world.seed(title="Survivor")

    res = world.api.delete(world.doc_path(doc.id), world.t.owner)
    assert res.status == 202, res.body
    assert set(res.body) == {"status", "purge_after"}
    assert res.body["status"] == "deletion_requested"
    row = world.row("SELECT deleted_at, deleted_by FROM documents WHERE id = :d", d=doc.id)
    assert row.deleted_by == world.t.owner.id
    grace = Settings.model_fields["DOCUMENT_PURGE_GRACE_DAYS"].default
    assert datetime.fromisoformat(res.body["purge_after"]) == row.deleted_at + timedelta(days=grace)
    meta = assert_success_audit(
        world.api, res, "document.deleted", actor=world.t.owner, org_id=world.org_id
    )
    assert meta["workspace_id"] == str(world.ws)
    assert meta["title"] == "Doomed"
    audit_row = world.api.audit(res.request_id)[0]
    assert (audit_row["target_type"], audit_row["target_id"]) == ("document", str(doc.id))

    # 404 for every caller on every path, the owner included.
    version_id = doc.versions[-1]
    everyone = [*world.everyone.values()]
    for user in everyone:
        for path in (
            world.doc_path(doc.id),
            world.doc_path(doc.id, "/versions"),
            world.doc_path(doc.id, f"/versions/{version_id}"),
            world.doc_path(doc.id, f"/versions/{version_id}/content-url"),
        ):
            res = world.api.get(path, user)
            assert (res.status, res.body) == (404, {"detail": "not_found"}), (user.email, path)
        assert world.api.patch(world.doc_path(doc.id), user, {"title": "x"}).status == 404
        assert world.api.delete(world.doc_path(doc.id), user).status == 404  # second DELETE
        listed = world.api.get(world.path("/documents"), user)
        assert [i["id"] for i in listed.body["items"]] == [str(other.id)]
    # Even a manage-grant holder.
    world.grant(doc.id, world.plain, "manage")
    assert world.api.get(world.doc_path(doc.id), world.plain).status == 404
    assert world.api.delete(world.doc_path(doc.id), world.plain).status == 404

    # The deletion is logical: rows and objects are untouched (the purge is a later job).
    assert (
        world.row("SELECT count(*) AS n FROM document_versions WHERE document_id = :d", d=doc.id).n
        == 2
    )
    assert (
        world.row(
            "SELECT count(*) AS n FROM document_versions"
            " WHERE document_id = :d AND purged_at IS NOT NULL",
            d=doc.id,
        ).n
        == 0
    )
    assert set(keys) <= set(store.keys)
    for key in keys:
        assert store.read(key) == b"bytes of " + key.encode()
    # Exactly one document.deleted event, none for the rejected repeats.
    assert (
        world.row(
            "SELECT count(*) AS n FROM audit_events WHERE event_type = 'document.deleted'"
            " AND target_id = :d",
            d=str(doc.id),
        ).n
        == 1
    )


def _sha(data: bytes) -> str:
    import base64
    import hashlib

    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_second_delete_is_404_and_does_not_move_the_purge_date(world: World) -> None:
    doc = world.seed(title="Once")
    first = world.api.delete(world.doc_path(doc.id), world.t.admin)
    assert first.status == 202
    stamp = world.row("SELECT deleted_at FROM documents WHERE id = :d", d=doc.id).deleted_at
    again = world.api.delete(world.doc_path(doc.id), world.t.owner)
    assert (again.status, again.body) == (404, {"detail": "not_found"})
    assert world.row("SELECT deleted_at FROM documents WHERE id = :d", d=doc.id).deleted_at == stamp
    assert world.api.audit(again.request_id) == []


def test_delete_role_matrix_and_grants(world: World) -> None:
    for name in ("owner", "admin", "ws_admin"):
        doc = world.seed(title=f"by {name}")
        res = world.api.delete(world.doc_path(doc.id), world.everyone[name])
        assert res.status == 202, (name, res.body)
    doc = world.seed(title="Protected")
    path = world.doc_path(doc.id)
    for user in (world.editor, world.viewer):
        res = world.api.delete(path, user)
        assert_denied(world.api, res, 403, action="document.manage", reason="insufficient_role")
    res = world.api.delete(path, world.plain)
    assert_denied(world.api, res, 404, action="document.manage", reason="not_workspace_member")
    res = world.api.delete(path, world.t.outsider)
    assert_denied(world.api, res, 404, action="document.manage", reason="not_org_member")
    assert world.api.delete(path, None).status == 401
    # A write grant is not enough, a manage grant is, an expired or revoked one is not.
    world.grant(doc.id, world.plain, "write")
    assert_denied(
        world.api,
        world.api.delete(path, world.plain),
        403,
        action="document.manage",
        reason="insufficient_role",
    )
    world.grant(doc.id, world.other_plain, "manage", expires_in="-1 hour")
    assert world.api.delete(path, world.other_plain).status == 404
    assert world.row("SELECT deleted_at FROM documents WHERE id = :d", d=doc.id).deleted_at is None
    world.grant(doc.id, world.editor, "manage")  # the editor gains manage by grant
    assert world.api.delete(path, world.editor).status == 202
    foreign = world.seed_foreign(title="Foreign")
    assert world.api.delete(world.doc_path(foreign.id), world.t.owner).status == 404
    assert (
        world.row("SELECT deleted_at FROM documents WHERE id = :d", d=foreign.id).deleted_at is None
    )


def test_delete_unknown_document_is_404(world: World) -> None:
    res = world.api.delete(world.doc_path(uuid.uuid4()), world.t.owner)
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert world.api.delete(world.doc_path("nope"), world.t.owner).status == 422


def test_deleted_documents_leave_the_database_flags_consistent(world: World) -> None:
    doc = world.seed(title="Flags")
    world.api.delete(world.doc_path(doc.id), world.ws_admin)
    row = world.row(
        "SELECT (deleted_at IS NULL) = (deleted_by IS NULL) AS consistent, deleted_by"
        " FROM documents WHERE id = :d",
        d=doc.id,
    )
    assert row.consistent is True
    assert row.deleted_by == world.ws_admin.id
    with world.engine.connect() as conn:
        # The version rows are still there for the purge job.
        n = conn.execute(
            text("SELECT count(*) FROM document_versions WHERE document_id = :d"), {"d": doc.id}
        ).scalar()
    assert n == 1
