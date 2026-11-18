"""Workspace endpoints (M01.02.04): CRUD, memberships, role matrix, soft delete, audit."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from tenancy_support import Api, Tenant, User, assert_denied, assert_success_audit, new_slug


@dataclass(frozen=True)
class WsFixture:
    t: Tenant
    ws: uuid.UUID
    ws_admin: User  # org member with workspace role admin
    editor: User
    viewer: User
    plain: User  # org member without a workspace role

    def path(self, sub: str = "") -> str:
        return f"/v1/orgs/{self.t.org_id}/workspaces/{self.ws}{sub}"


def _setup(api: Api) -> WsFixture:
    t = api.tenant()
    ws = api.create_workspace(t.org_id, t.owner)
    people: dict[str, User] = {}
    for role in ("admin", "editor", "viewer"):
        user = api.user(f"ws{role}")
        api.set_role(t.org_id, t.owner, user, "member")
        res = api.put(
            f"/v1/orgs/{t.org_id}/workspaces/{ws}/members/{user.id}", t.owner, {"role": role}
        )
        assert res.status == 200, res.body
        people[role] = user
    return WsFixture(t, ws, people["admin"], people["editor"], people["viewer"], t.member)


def test_create_workspace_role_matrix(api: Api) -> None:
    t = api.tenant()
    path = f"/v1/orgs/{t.org_id}/workspaces"
    for user in (t.owner, t.admin):
        slug = new_slug("ws")
        res = api.post(path, user, {"slug": slug, "name": " Research "})
        assert res.status == 201, res.body
        body = res.body
        assert set(body) == {"id", "org_id", "slug", "name", "created_at", "role"}
        assert (body["org_id"], body["slug"], body["name"], body["role"]) == (
            str(t.org_id),
            slug,
            "Research",
            "admin",
        )
        datetime.fromisoformat(body["created_at"])
        meta = assert_success_audit(api, res, "workspace.created", actor=user, org_id=t.org_id)
        assert meta == {"slug": slug, "name": "Research"}
        row = api.audit(res.request_id)[0]
        assert (row["target_type"], row["target_id"]) == ("workspace", body["id"])
        assert (
            api.scalar(
                "SELECT role FROM workspace_memberships WHERE workspace_id = :w AND user_id = :u",
                w=body["id"],
                u=user.id,
            )
            == "admin"
        )
    body = {"slug": new_slug("ws"), "name": "x"}
    assert_denied(
        api,
        api.post(path, t.member, body),
        403,
        action="workspace.create",
        reason="insufficient_role",
    )
    assert_denied(
        api,
        api.post(path, t.outsider, body),
        404,
        action="workspace.create",
        reason="not_org_member",
    )
    assert api.scalar("SELECT count(*) FROM workspaces WHERE slug = :s", s=body["slug"]) == 0


def test_workspace_slug_taken_per_org_including_case_variant(api: Api) -> None:
    a = api.tenant()
    b = api.tenant()
    slug = new_slug("ws")
    api.create_workspace(a.org_id, a.owner, slug)
    res = api.post(f"/v1/orgs/{a.org_id}/workspaces", a.admin, {"slug": slug, "name": "x"})
    assert (res.status, res.body) == (409, {"detail": "slug_taken"})
    assert api.audit(res.request_id) == []
    # Upper-case is invalid input (422), never "taken" (DC-018).
    res = api.post(f"/v1/orgs/{a.org_id}/workspaces", a.admin, {"slug": slug.upper(), "name": "x"})
    assert (res.status, res.body["detail"]) == (422, "invalid_request")
    assert api.audit(res.request_id) == []
    # The same slug in another org is fine.
    api.create_workspace(b.org_id, b.owner, slug)
    res = api.post(f"/v1/orgs/{a.org_id}/workspaces", a.owner, {"slug": "x", "name": "x"})
    assert (res.status, res.body["detail"]) == (422, "invalid_request")


def test_list_workspaces_is_filtered_to_readable_ones(api: Api) -> None:
    f = _setup(api)
    other = api.create_workspace(f.t.org_id, f.t.admin)
    path = f"/v1/orgs/{f.t.org_id}/workspaces"

    # Org owners/admins see every workspace; `role` is their explicit workspace role or null.
    for manager, roles in ((f.t.owner, ["admin", None]), (f.t.admin, [None, "admin"])):
        res = api.get(path, manager)
        assert res.status == 200
        assert [(w["id"], w["role"]) for w in res.body["items"]] == [
            (str(f.ws), roles[0]),
            (str(other), roles[1]),
        ]
        assert all("role" in w for w in res.body["items"])
        assert res.body["next_cursor"] is None
    for user, role in ((f.ws_admin, "admin"), (f.editor, "editor"), (f.viewer, "viewer")):
        res = api.get(path, user)
        assert [(w["id"], w["role"]) for w in res.body["items"]] == [(str(f.ws), role)]
    assert api.get(path, f.plain).body == {"items": [], "next_cursor": None}
    assert_denied(api, api.get(path, f.t.outsider), 404, action="org.read", reason="not_org_member")


def test_list_workspaces_pagination(api: Api) -> None:
    t = api.tenant()
    created = [api.create_workspace(t.org_id, t.owner) for _ in range(5)]
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        res = api.get(f"/v1/orgs/{t.org_id}/workspaces", t.admin, params=params)
        assert res.status == 200
        seen += [w["id"] for w in res.body["items"]]
        cursor = res.body["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(w) for w in created]
    res = api.get(f"/v1/orgs/{t.org_id}/workspaces", t.admin, params={"cursor": "%%%"})
    assert (res.status, res.body) == (422, {"detail": "invalid_cursor"})


def test_get_workspace_role_matrix(api: Api) -> None:
    f = _setup(api)
    for user, role in (
        (f.t.owner, "admin"),
        (f.t.admin, None),  # org admin without a workspace membership: key present, null
        (f.ws_admin, "admin"),
        (f.editor, "editor"),
        (f.viewer, "viewer"),
    ):
        res = api.get(f.path(), user)
        assert res.status == 200, user
        assert (res.body["id"], res.body["org_id"], res.body["role"]) == (
            str(f.ws),
            str(f.t.org_id),
            role,
        )
    res = api.get(f.path(), f.plain)
    assert_denied(api, res, 404, action="workspace.read", reason="not_workspace_member")
    row = api.audit(res.request_id)[0]
    assert (row["org_id"], row["target_type"], row["target_id"]) == (
        f.t.org_id,
        "workspace",
        str(f.ws),
    )
    assert_denied(
        api, api.get(f.path(), f.t.outsider), 404, action="workspace.read", reason="not_org_member"
    )
    res = api.get(f"/v1/orgs/{f.t.org_id}/workspaces/{uuid.uuid4()}", f.t.owner)
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert api.audit(res.request_id) == []


def test_patch_workspace_role_matrix(api: Api) -> None:
    f = _setup(api)
    for user in (f.t.owner, f.t.admin, f.ws_admin):
        name = f"By {user.name}"
        res = api.patch(f.path(), user, {"name": name})
        assert (res.status, res.body["name"]) == (200, name)
        meta = assert_success_audit(api, res, "workspace.updated", actor=user, org_id=f.t.org_id)
        assert meta["to_name"] == name
    res = api.patch(f.path(), f.t.owner, {})
    assert res.status == 200
    assert api.audit(res.request_id) == []
    for user in (f.editor, f.viewer):
        assert_denied(
            api,
            api.patch(f.path(), user, {"name": "x"}),
            403,
            action="workspace.update",
            reason="insufficient_role",
        )
    assert_denied(
        api,
        api.patch(f.path(), f.plain, {"name": "x"}),
        404,
        action="workspace.update",
        reason="not_workspace_member",
    )
    assert_denied(
        api,
        api.patch(f.path(), f.t.outsider, {"name": "x"}),
        404,
        action="workspace.update",
        reason="not_org_member",
    )
    assert api.get(f.path(), f.viewer).body["name"] == f"By {f.ws_admin.name}"


def test_delete_workspace_role_matrix_and_soft_delete(api: Api) -> None:
    f = _setup(api)
    for user in (f.editor, f.viewer):
        assert_denied(
            api,
            api.delete(f.path(), user),
            403,
            action="workspace.delete",
            reason="insufficient_role",
        )
    assert_denied(
        api,
        api.delete(f.path(), f.plain),
        404,
        action="workspace.delete",
        reason="not_workspace_member",
    )
    assert_denied(
        api,
        api.delete(f.path(), f.t.outsider),
        404,
        action="workspace.delete",
        reason="not_org_member",
    )

    res = api.delete(f.path(), f.ws_admin)
    assert (res.status, res.body) == (204, None)
    slug = api.scalar("SELECT slug FROM workspaces WHERE id = :w", w=f.ws)
    meta = assert_success_audit(api, res, "workspace.deleted", actor=f.ws_admin, org_id=f.t.org_id)
    assert meta == {"slug": slug}

    for user in (f.t.owner, f.t.admin, f.ws_admin, f.editor, f.viewer):
        assert api.get(f"/v1/orgs/{f.t.org_id}/workspaces", user).body["items"] == []
        for method, sub, body in [
            ("GET", "", None),
            ("PATCH", "", {"name": "x"}),
            ("DELETE", "", None),
            ("PUT", f"/members/{f.plain.id}", {"role": "viewer"}),
            ("DELETE", f"/members/{f.editor.id}", None),
        ]:
            res = api.call(method, f.path(sub), user, json=body)
            assert (res.status, res.body) == (404, {"detail": "not_found"}), (user, method, sub)
            assert api.audit(res.request_id) == []
    # The org itself is unaffected; the slug stays reserved in this org.
    assert api.get(f"/v1/orgs/{f.t.org_id}", f.t.owner).status == 200
    res = api.post(f"/v1/orgs/{f.t.org_id}/workspaces", f.t.owner, {"slug": slug, "name": "x"})
    assert (res.status, res.body) == (409, {"detail": "slug_taken"})


def test_workspace_membership_crud_and_audit(api: Api) -> None:
    f = _setup(api)
    target = f.plain
    path = f.path(f"/members/{target.id}")

    res = api.put(path, f.ws_admin, {"role": "viewer"})
    assert res.status == 200
    assert res.body == {
        "user_id": str(target.id),
        "email": target.email,
        "display_name": target.name,
        "role": "viewer",
    }
    meta = assert_success_audit(
        api, res, "workspace.member.added", actor=f.ws_admin, org_id=f.t.org_id
    )
    assert meta == {
        "workspace_id": str(f.ws),
        "email": target.email,
        "from_role": None,
        "to_role": "viewer",
    }
    assert api.get(f.path(), target).body["role"] == "viewer"

    res = api.put(path, f.t.admin, {"role": "editor"})
    assert (res.status, res.body["role"]) == (200, "editor")
    meta = assert_success_audit(
        api, res, "workspace.member.role_changed", actor=f.t.admin, org_id=f.t.org_id
    )
    assert (meta["from_role"], meta["to_role"]) == ("viewer", "editor")

    res = api.put(path, f.t.admin, {"role": "editor"})
    assert res.status == 200
    assert api.audit(res.request_id) == []

    res = api.delete(path, f.t.owner)
    assert (res.status, res.body) == (204, None)
    meta = assert_success_audit(
        api, res, "workspace.member.removed", actor=f.t.owner, org_id=f.t.org_id
    )
    assert meta == {
        "workspace_id": str(f.ws),
        "email": target.email,
        "from_role": "editor",
        "to_role": None,
    }
    assert api.get(f.path(), target).status == 404
    res = api.delete(path, f.t.owner)
    assert (res.status, res.body) == (404, {"detail": "not_found"})
    assert api.audit(res.request_id) == []
    # Still an org member.
    assert api.org_role(f.t.org_id, target) == "member"


def test_workspace_membership_permissions(api: Api) -> None:
    f = _setup(api)
    path = f.path(f"/members/{f.plain.id}")
    for user in (f.editor, f.viewer):
        assert_denied(
            api,
            api.put(path, user, {"role": "viewer"}),
            403,
            action="workspace.members.manage",
            reason="insufficient_role",
        )
        assert_denied(
            api,
            api.delete(f.path(f"/members/{f.ws_admin.id}"), user),
            403,
            action="workspace.members.manage",
            reason="insufficient_role",
        )
    assert_denied(
        api,
        api.put(path, f.plain, {"role": "admin"}),
        404,
        action="workspace.members.manage",
        reason="not_workspace_member",
    )
    assert_denied(
        api,
        api.put(path, f.t.outsider, {"role": "admin"}),
        404,
        action="workspace.members.manage",
        reason="not_org_member",
    )
    assert api.get(f.path(), f.plain).status == 404


def test_put_workspace_member_requires_org_membership(api: Api) -> None:
    f = _setup(api)
    for target in (f.t.outsider.id, api.user().id, uuid.uuid4()):
        res = api.put(f.path(f"/members/{target}"), f.t.owner, {"role": "viewer"})
        assert (res.status, res.body) == (422, {"detail": "not_org_member"})
        assert api.audit(res.request_id) == []
    res = api.put(f.path(f"/members/{f.plain.id}"), f.t.owner, {"role": "owner"})
    assert (res.status, res.body["detail"]) == (422, "invalid_request")
    assert (
        api.scalar("SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w", w=f.ws)
        == 4
    )
