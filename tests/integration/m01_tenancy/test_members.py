"""Organization membership endpoints (M01.02.02): roles, owner-only rules, last owner, audit."""

from __future__ import annotations

import uuid

from tenancy_support import Api, assert_denied, assert_success_audit


def _members(api: Api, org_id: uuid.UUID, user: object) -> dict[str, str]:
    res = api.get(f"/v1/orgs/{org_id}/members", user)  # type: ignore[arg-type]
    assert res.status == 200, res.body
    return {m["user_id"]: m["role"] for m in res.body["items"]}


def test_list_members_role_matrix_and_shape(api: Api) -> None:
    t = api.tenant()
    path = f"/v1/orgs/{t.org_id}/members"
    for user in (t.owner, t.admin, t.member):
        res = api.get(path, user)
        assert res.status == 200
        assert res.body["next_cursor"] is None
        items = {m["user_id"]: m for m in res.body["items"]}
        assert items[str(t.admin.id)] == {
            "user_id": str(t.admin.id),
            "email": t.admin.email,
            "display_name": t.admin.name,
            "role": "admin",
        }
        assert {k: v["role"] for k, v in items.items()} == {
            str(t.owner.id): "owner",
            str(t.admin.id): "admin",
            str(t.member.id): "member",
        }
    assert_denied(
        api, api.get(path, t.outsider), 404, action="org.members.read", reason="not_org_member"
    )


def test_members_pagination(api: Api) -> None:
    t = api.tenant()
    for _ in range(3):
        api.set_role(t.org_id, t.owner, api.user(), "member")
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        res = api.get(f"/v1/orgs/{t.org_id}/members", t.member, params=params)
        assert res.status == 200
        seen += [m["user_id"] for m in res.body["items"]]
        cursor = res.body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 6
    assert seen[:3] == [str(t.owner.id), str(t.admin.id), str(t.member.id)]


def test_owner_adds_and_changes_roles_with_audit(api: Api) -> None:
    t = api.tenant()
    carol = api.user("carol")
    path = f"/v1/orgs/{t.org_id}/members/{carol.id}"

    res = api.put(path, t.owner, {"role": "member"})
    assert res.status == 200
    assert res.body == {
        "user_id": str(carol.id),
        "email": carol.email,
        "display_name": carol.name,
        "role": "member",
    }
    meta = assert_success_audit(api, res, "org.member.added", actor=t.owner, org_id=t.org_id)
    assert meta == {"email": carol.email, "from_role": None, "to_role": "member"}
    row = api.audit(res.request_id)[0]
    assert (row["target_type"], row["target_id"]) == ("user", str(carol.id))

    res = api.put(path, t.owner, {"role": "owner"})
    assert (res.status, res.body["role"]) == (200, "owner")
    meta = assert_success_audit(api, res, "org.member.role_changed", actor=t.owner, org_id=t.org_id)
    assert meta == {"email": carol.email, "from_role": "member", "to_role": "owner"}

    # Same role again: 200, nothing changed, no audit row.
    res = api.put(path, t.owner, {"role": "owner"})
    assert (res.status, res.body["role"]) == (200, "owner")
    assert api.audit(res.request_id) == []

    # Carol now sees the org.
    assert [o["role"] for o in api.get("/v1/orgs", carol).body["items"]] == ["owner"]


def test_admin_manages_members_but_never_owner(api: Api) -> None:
    t = api.tenant()
    dave = api.user("dave")
    path = f"/v1/orgs/{t.org_id}/members/{dave.id}"

    res = api.put(path, t.admin, {"role": "member"})
    assert res.status == 200
    assert_success_audit(api, res, "org.member.added", actor=t.admin, org_id=t.org_id)
    res = api.put(path, t.admin, {"role": "admin"})
    assert (res.status, res.body["role"]) == (200, "admin")

    # Admin may not grant owner ...
    assert_denied(
        api,
        api.put(path, t.admin, {"role": "owner"}),
        403,
        action="org.members.manage",
        reason="insufficient_role",
    )
    # ... nor demote or remove an owner.
    owner_path = f"/v1/orgs/{t.org_id}/members/{t.owner.id}"
    assert_denied(
        api,
        api.put(owner_path, t.admin, {"role": "member"}),
        403,
        action="org.members.manage",
        reason="insufficient_role",
    )
    assert_denied(
        api,
        api.delete(owner_path, t.admin),
        403,
        action="org.members.manage",
        reason="insufficient_role",
    )
    assert api.org_role(t.org_id, t.owner) == "owner"
    assert api.org_role(t.org_id, dave) == "admin"

    # Admin removes another admin.
    res = api.delete(path, t.admin)
    assert res.status == 204
    meta = assert_success_audit(api, res, "org.member.removed", actor=t.admin, org_id=t.org_id)
    assert meta == {"email": dave.email, "from_role": "admin", "to_role": None}
    assert api.org_role(t.org_id, dave) is None
    assert api.get(f"/v1/orgs/{t.org_id}", dave).status == 404


def test_members_and_outsiders_cannot_manage(api: Api) -> None:
    t = api.tenant()
    erin = api.user("erin")
    path = f"/v1/orgs/{t.org_id}/members/{erin.id}"
    assert_denied(
        api,
        api.put(path, t.member, {"role": "member"}),
        403,
        action="org.members.manage",
        reason="insufficient_role",
    )
    assert_denied(
        api,
        api.put(path, t.outsider, {"role": "member"}),
        404,
        action="org.members.manage",
        reason="not_org_member",
    )
    # An outsider can't add themselves either.
    assert_denied(
        api,
        api.put(f"/v1/orgs/{t.org_id}/members/{t.outsider.id}", t.outsider, {"role": "owner"}),
        404,
        action="org.members.manage",
        reason="not_org_member",
    )
    assert_denied(
        api,
        api.delete(f"/v1/orgs/{t.org_id}/members/{t.admin.id}", t.member),
        403,
        action="org.members.manage",
        reason="insufficient_role",
    )
    assert_denied(
        api,
        api.delete(f"/v1/orgs/{t.org_id}/members/{t.member.id}", t.outsider),
        404,
        action="org.members.manage",
        reason="not_org_member",
    )
    assert api.org_role(t.org_id, erin) is None
    assert api.org_role(t.org_id, t.outsider) is None
    assert _members(api, t.org_id, t.owner)[str(t.admin.id)] == "admin"


def test_put_member_requires_an_existing_user_and_valid_role(api: Api) -> None:
    t = api.tenant()
    res = api.put(f"/v1/orgs/{t.org_id}/members/{uuid.uuid4()}", t.owner, {"role": "member"})
    assert (res.status, res.body) == (404, {"detail": "user_not_found"})
    assert api.audit(res.request_id) == []
    for body in ({"role": "superuser"}, {"role": "viewer"}, {}):
        res = api.put(f"/v1/orgs/{t.org_id}/members/{t.member.id}", t.owner, body)
        assert (res.status, res.body["detail"]) == (422, "invalid_request")
    assert api.org_role(t.org_id, t.member) == "member"


def test_delete_unknown_membership_is_404(api: Api) -> None:
    t = api.tenant()
    stranger = api.user()
    for target in (stranger.id, uuid.uuid4()):
        res = api.delete(f"/v1/orgs/{t.org_id}/members/{target}", t.owner)
        assert (res.status, res.body) == (404, {"detail": "not_found"})
        assert api.audit(res.request_id) == []


def test_last_owner_cannot_be_demoted_or_removed(api: Api) -> None:
    t = api.tenant()
    own = f"/v1/orgs/{t.org_id}/members/{t.owner.id}"
    for role in ("admin", "member"):
        res = api.put(own, t.owner, {"role": role})
        assert (res.status, res.body) == (409, {"detail": "last_owner"})
        assert api.audit(res.request_id) == []  # the role_changed event was rolled back
    res = api.delete(own, t.owner)
    assert (res.status, res.body) == (409, {"detail": "last_owner"})
    assert api.audit(res.request_id) == []
    assert api.org_role(t.org_id, t.owner) == "owner"

    # With a second owner, the first may step down, and then the second is the last owner.
    api.set_role(t.org_id, t.owner, t.admin, "owner")
    res = api.put(own, t.owner, {"role": "member"})
    assert (res.status, res.body["role"]) == (200, "member")
    assert_success_audit(api, res, "org.member.role_changed", actor=t.owner, org_id=t.org_id)
    admin_path = f"/v1/orgs/{t.org_id}/members/{t.admin.id}"
    res = api.delete(admin_path, t.admin)
    assert (res.status, res.body) == (409, {"detail": "last_owner"})
    assert api.org_role(t.org_id, t.admin) == "owner"


def test_removing_a_member_removes_their_workspace_memberships(api: Api) -> None:
    t = api.tenant()
    ws = api.create_workspace(t.org_id, t.owner)
    res = api.put(
        f"/v1/orgs/{t.org_id}/workspaces/{ws}/members/{t.member.id}", t.owner, {"role": "editor"}
    )
    assert res.status == 200
    assert api.get(f"/v1/orgs/{t.org_id}/workspaces/{ws}", t.member).status == 200
    assert api.delete(f"/v1/orgs/{t.org_id}/members/{t.member.id}", t.owner).status == 204
    assert (
        api.scalar(
            "SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w AND user_id = :u",
            w=ws,
            u=t.member.id,
        )
        == 0
    )
    assert api.get(f"/v1/orgs/{t.org_id}/workspaces/{ws}", t.member).status == 404
