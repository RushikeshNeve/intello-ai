"""Organization endpoints (M01.02.02): CRUD, role matrix, slugs, soft delete, pagination, audit."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from tenancy_support import Api, assert_denied, assert_success_audit, new_slug


def test_create_org_makes_the_creator_owner_atomically(api: Api) -> None:
    alice = api.user("alice")
    slug = new_slug()
    res = api.post("/v1/orgs", alice, {"slug": slug, "name": "  Acme Corp  "})
    assert res.status == 201, res.body
    body = res.body
    assert set(body) == {"id", "slug", "name", "created_at", "role"}
    assert (body["slug"], body["name"], body["role"]) == (slug, "Acme Corp", "owner")
    datetime.fromisoformat(body["created_at"])
    org_id = uuid.UUID(body["id"])

    assert api.org_role(org_id, alice) == "owner"
    assert api.scalar("SELECT created_by FROM organizations WHERE id = :o", o=org_id) == alice.id
    meta = assert_success_audit(api, res, "org.created", actor=alice, org_id=org_id)
    assert meta == {"slug": slug, "name": "Acme Corp"}

    listed = api.get("/v1/orgs", alice)
    assert listed.status == 200
    assert listed.body == {
        "items": [{"id": str(org_id), "slug": slug, "name": "Acme Corp", "role": "owner"}],
        "next_cursor": None,
    }
    got = api.get(f"/v1/orgs/{org_id}", alice)
    assert (got.status, got.body) == (200, body)


def test_slug_taken_and_uppercase_is_invalid_not_taken(api: Api) -> None:
    alice, bob = api.user("alice"), api.user("bob")
    slug = new_slug()
    api.create_org(alice, slug)
    before = api.scalar("SELECT count(*) FROM organizations")
    # A real collision is 409 slug_taken and nothing is written.
    res = api.post("/v1/orgs", bob, {"slug": slug, "name": "Clash"})
    assert (res.status, res.body) == (409, {"detail": "slug_taken"})
    assert api.audit(res.request_id) == []  # rolled back: no org.created
    # Upper-case variants are invalid input (422), never reported as "taken" (DC-018): an
    # upper-case slug with no twin must not claim a slug is taken, and must not reveal one.
    for candidate in (slug.upper(), slug[:4].upper() + slug[4:]):
        res = api.post("/v1/orgs", bob, {"slug": candidate, "name": "Clash"})
        assert (res.status, res.body["detail"]) == (422, "invalid_request"), candidate
        assert api.audit(res.request_id) == []
    assert api.scalar("SELECT count(*) FROM organizations") == before
    assert api.get("/v1/orgs", bob).body["items"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "a", "name": "x"},
        {"slug": "-leading-dash", "name": "x"},
        {"slug": "has space", "name": "x"},
        {"slug": "x" * 64, "name": "x"},
        {"slug": "fine-slug", "name": "   "},
        {"slug": "fine-slug", "name": "x" * 201},
        {"slug": "fine-slug"},
        {"slug": "fine-slug", "name": "x", "id": str(uuid.uuid4())},
    ],
)
def test_create_org_validation(api: Api, body: dict[str, str]) -> None:
    res = api.post("/v1/orgs", api.user(), body)
    assert res.status == 422
    assert res.body["detail"] == "invalid_request"


def test_authentication_is_required_everywhere(api: Api) -> None:
    org = uuid.uuid4()
    ws = uuid.uuid4()
    user = uuid.uuid4()
    for method, path in [
        ("GET", "/v1/orgs"),
        ("POST", "/v1/orgs"),
        ("GET", f"/v1/orgs/{org}"),
        ("PATCH", f"/v1/orgs/{org}"),
        ("DELETE", f"/v1/orgs/{org}"),
        ("GET", f"/v1/orgs/{org}/members"),
        ("PUT", f"/v1/orgs/{org}/members/{user}"),
        ("DELETE", f"/v1/orgs/{org}/members/{user}"),
        ("GET", f"/v1/orgs/{org}/workspaces"),
        ("POST", f"/v1/orgs/{org}/workspaces"),
        ("GET", f"/v1/orgs/{org}/workspaces/{ws}"),
        ("PATCH", f"/v1/orgs/{org}/workspaces/{ws}"),
        ("DELETE", f"/v1/orgs/{org}/workspaces/{ws}"),
        ("PUT", f"/v1/orgs/{org}/workspaces/{ws}/members/{user}"),
        ("DELETE", f"/v1/orgs/{org}/workspaces/{ws}/members/{user}"),
    ]:
        res = api.call(method, path, None, json={"role": "member"})
        assert (res.status, res.body) == (401, {"detail": "invalid_token"}), (method, path)


def test_ids_are_validated_as_uuid(api: Api) -> None:
    alice = api.user()
    org = api.create_org(alice)
    for path in (
        "/v1/orgs/not-a-uuid",
        "/v1/orgs/1/members",
        f"/v1/orgs/{org}/members/42",
        f"/v1/orgs/{org}/workspaces/abc",
    ):
        res = api.get(path, alice) if not path.endswith("/42") else api.delete(path, alice)
        assert res.status == 422, path
        assert res.body["detail"] == "invalid_request"


def test_get_org_role_matrix(api: Api) -> None:
    t = api.tenant()
    path = f"/v1/orgs/{t.org_id}"
    for user, role in ((t.owner, "owner"), (t.admin, "admin"), (t.member, "member")):
        res = api.get(path, user)
        assert res.status == 200
        assert (res.body["id"], res.body["slug"], res.body["role"]) == (str(t.org_id), t.slug, role)
        assert api.audit(res.request_id) == []
    res = api.get(path, t.outsider)
    assert_denied(api, res, 404, action="org.read", reason="not_org_member")
    row = api.audit(res.request_id)[0]
    assert (row["org_id"], row["actor_user_id"]) == (t.org_id, t.outsider.id)
    assert (row["target_type"], row["target_id"]) == ("organization", str(t.org_id))


def test_patch_org_role_matrix(api: Api) -> None:
    t = api.tenant()
    path = f"/v1/orgs/{t.org_id}"
    res = api.patch(path, t.owner, {"name": "Renamed by owner"})
    assert (res.status, res.body["name"], res.body["role"]) == (200, "Renamed by owner", "owner")
    meta = assert_success_audit(api, res, "org.updated", actor=t.owner, org_id=t.org_id)
    assert meta == {"from_name": "Org", "to_name": "Renamed by owner"}

    res = api.patch(path, t.admin, {"name": "Renamed by admin"})
    assert (res.status, res.body["name"], res.body["role"]) == (200, "Renamed by admin", "admin")
    assert_success_audit(api, res, "org.updated", actor=t.admin, org_id=t.org_id)

    # Same name or no name: no change, no audit row.
    for body in ({"name": "Renamed by admin"}, {}):
        res = api.patch(path, t.owner, body)
        assert (res.status, res.body["name"]) == (200, "Renamed by admin")
        assert api.audit(res.request_id) == []

    assert_denied(
        api,
        api.patch(path, t.member, {"name": "x"}),
        403,
        action="org.update",
        reason="insufficient_role",
    )
    assert_denied(
        api,
        api.patch(path, t.outsider, {"name": "x"}),
        404,
        action="org.update",
        reason="not_org_member",
    )
    assert api.get(path, t.owner).body["name"] == "Renamed by admin"
    res = api.patch(path, t.owner, {"slug": "new-slug"})
    assert res.status == 422


def test_delete_org_role_matrix_and_soft_delete_is_404_everywhere(api: Api) -> None:
    t = api.tenant()
    ws = api.create_workspace(t.org_id, t.owner)
    path = f"/v1/orgs/{t.org_id}"
    assert_denied(
        api, api.delete(path, t.admin), 403, action="org.delete", reason="insufficient_role"
    )
    assert_denied(
        api, api.delete(path, t.member), 403, action="org.delete", reason="insufficient_role"
    )
    assert_denied(
        api, api.delete(path, t.outsider), 404, action="org.delete", reason="not_org_member"
    )
    assert api.get(path, t.owner).status == 200

    res = api.delete(path, t.owner)
    assert (res.status, res.body) == (204, None)
    meta = assert_success_audit(api, res, "org.deleted", actor=t.owner, org_id=t.org_id)
    assert meta == {"slug": t.slug}
    assert api.scalar("SELECT deleted_at IS NOT NULL FROM organizations WHERE id = :o", o=t.org_id)
    assert api.org_role(t.org_id, t.owner) == "owner"  # memberships are kept (soft delete)

    for user in (t.owner, t.admin, t.member, t.outsider):
        assert str(t.org_id) not in {o["id"] for o in api.get("/v1/orgs", user).body["items"]}
        for method, sub, body in [
            ("GET", "", None),
            ("PATCH", "", {"name": "x"}),
            ("DELETE", "", None),
            ("GET", "/members", None),
            ("PUT", f"/members/{t.member.id}", {"role": "admin"}),
            ("DELETE", f"/members/{t.member.id}", None),
            ("GET", "/workspaces", None),
            ("POST", "/workspaces", {"slug": new_slug("ws"), "name": "x"}),
            ("GET", f"/workspaces/{ws}", None),
            ("PATCH", f"/workspaces/{ws}", {"name": "x"}),
            ("DELETE", f"/workspaces/{ws}", None),
            ("PUT", f"/workspaces/{ws}/members/{t.member.id}", {"role": "viewer"}),
            ("DELETE", f"/workspaces/{ws}/members/{t.owner.id}", None),
        ]:
            res = api.call(method, path + sub, user, json=body)
            assert (res.status, res.body) == (404, {"detail": "not_found"}), (user, method, sub)
            assert api.audit(res.request_id) == []
    # The slug stays reserved by the soft-deleted org.
    res = api.post("/v1/orgs", t.outsider, {"slug": t.slug, "name": "Reuse"})
    assert (res.status, res.body) == (409, {"detail": "slug_taken"})


def test_unknown_org_is_404_without_revealing_anything(api: Api) -> None:
    alice = api.user()
    res = api.get(f"/v1/orgs/{uuid.uuid4()}", alice)
    assert (res.status, res.body) == (404, {"detail": "not_found"})


def test_cross_org_id_guessing_is_404(api: Api) -> None:
    a = api.tenant()
    b = api.tenant()
    ws_a = api.create_workspace(a.org_id, a.owner)
    # b's owner can't read or touch org A, even with exact ids.
    for method, path, body in [
        ("GET", f"/v1/orgs/{a.org_id}", None),
        ("PATCH", f"/v1/orgs/{a.org_id}", {"name": "pwned"}),
        ("DELETE", f"/v1/orgs/{a.org_id}", None),
        ("GET", f"/v1/orgs/{a.org_id}/members", None),
        ("PUT", f"/v1/orgs/{a.org_id}/members/{b.owner.id}", {"role": "owner"}),
        ("DELETE", f"/v1/orgs/{a.org_id}/members/{a.owner.id}", None),
        ("GET", f"/v1/orgs/{a.org_id}/workspaces", None),
        ("GET", f"/v1/orgs/{a.org_id}/workspaces/{ws_a}", None),
    ]:
        res = api.call(method, path, b.owner, json=body)
        assert (res.status, res.body) == (404, {"detail": "not_found"}), (method, path)
        rows = api.audit(res.request_id)
        assert [r["event_type"] for r in rows] == ["authz.denied"]
        assert rows[0]["metadata"]["reason"] == "not_org_member"
    # Org A's workspace through org B's path: B's owner is an org manager there, still 404.
    for method, sub, body in [
        ("GET", "", None),
        ("PATCH", "", {"name": "pwned"}),
        ("DELETE", "", None),
        ("PUT", f"/members/{b.owner.id}", {"role": "admin"}),
    ]:
        res = api.call(method, f"/v1/orgs/{b.org_id}/workspaces/{ws_a}{sub}", b.owner, json=body)
        assert (res.status, res.body) == (404, {"detail": "not_found"}), (method, sub)
    # Org B's owner can add a user who exists (users are global) to org B, never to org A.
    res = api.put(f"/v1/orgs/{b.org_id}/members/{a.owner.id}", b.owner, {"role": "member"})
    assert res.status == 200
    assert api.org_role(a.org_id, b.owner) is None
    assert api.get(f"/v1/orgs/{a.org_id}", a.owner).body["name"] == "Org"


def test_list_orgs_pagination(api: Api) -> None:
    alice = api.user()
    created = [api.create_org(alice, name=f"Org {i}") for i in range(5)]
    other = api.user()
    api.create_org(other)  # never listed for alice
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        res = api.get("/v1/orgs", alice, params=params)
        assert res.status == 200
        pages += 1
        assert len(res.body["items"]) <= 2
        seen += [o["id"] for o in res.body["items"]]
        cursor = res.body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [str(o) for o in created]  # creation order, no gaps or repeats
    full = api.get("/v1/orgs", alice).body
    assert [o["id"] for o in full["items"]] == seen
    assert full["next_cursor"] is None
    assert len(api.get("/v1/orgs", alice, params={"limit": 200}).body["items"]) == 5


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"limit": "x"}])
def test_pagination_limit_bounds(api: Api, params: dict[str, object]) -> None:
    res = api.get("/v1/orgs", api.user(), params=params)
    assert (res.status, res.body["detail"]) == (422, "invalid_request")


@pytest.mark.parametrize("cursor", ["garbage!", "Zm9v", "MjAyNi0wMS0wMXxub3QtYS11dWlk"])
def test_invalid_cursor(api: Api, cursor: str) -> None:
    res = api.get("/v1/orgs", api.user(), params={"cursor": cursor})
    assert (res.status, res.body) == (422, {"detail": "invalid_cursor"})


def test_request_id_header_is_used_for_audit_and_unsafe_ones_are_replaced(api: Api) -> None:
    alice = api.user()
    res = api.client.post(
        "/v1/orgs",
        headers={"Authorization": f"Bearer {alice.token}", "X-Request-ID": "bad id with spaces"},
        json={"slug": new_slug(), "name": "x"},
    )
    assert res.status_code == 201
    stored = api.scalar(
        "SELECT request_id FROM audit_events WHERE org_id = :o AND event_type = 'org.created'",
        o=res.json()["id"],
    )
    uuid.UUID(stored)  # replaced by a generated UUID
