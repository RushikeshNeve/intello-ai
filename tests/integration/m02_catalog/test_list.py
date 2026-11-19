"""M02.02.02: ``GET {ws}/documents`` - visibility, grants, filters, sorting, keyset pagination."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from catalog_support import World
from sqlalchemy import event
from sqlalchemy.engine import Engine as SAEngine
from tenancy_support import Response, assert_denied

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

DOCUMENT_KEYS = {
    "id",
    "org_id",
    "workspace_id",
    "title",
    "created_by",
    "created_at",
    "updated_at",
    "version_count",
    "latest_version",
    "permissions",
    "ingestion",  # M03.03.04, additive (m03-contracts.md §11)
}
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


def ids(res: Response) -> list[str]:
    assert res.status == 200, res.body
    return [item["id"] for item in res.body["items"]]


# --- role matrix ----------------------------------------------------------------------------


def test_every_workspace_reader_sees_all_live_documents(world: World) -> None:
    a = world.seed(title="A")
    b = world.seed(title="B", versions=3)
    world.seed(title="Deleted", deleted=True)
    world.seed_foreign(title="Foreign")
    for name, user in world.everyone.items():
        res = world.api.get(world.path("/documents"), user)
        assert res.status == 200, (name, res.body)
        assert set(res.body) == {"items", "next_cursor", "can_upload"}
        assert {i["id"] for i in res.body["items"]} == {str(a.id), str(b.id)}, name
        assert res.body["next_cursor"] is None
        for item in res.body["items"]:
            assert set(item) == DOCUMENT_KEYS
            assert set(item["latest_version"]) == VERSION_KEYS
            assert item["latest_version"]["retention"] == {"status": "current", "purge_after": None}
            assert item["org_id"] == str(world.org_id)
            assert item["workspace_id"] == str(world.ws)
    by_id = {
        i["id"]: i for i in world.api.get(world.path("/documents"), world.t.owner).body["items"]
    }
    assert by_id[str(a.id)]["version_count"] == 1
    assert by_id[str(b.id)]["version_count"] == 3
    assert by_id[str(b.id)]["latest_version"]["version_number"] == 3
    assert by_id[str(b.id)]["latest_version"]["id"] == str(b.versions[-1])


def test_can_upload_and_permissions_per_role(world: World) -> None:
    doc = world.seed(title="P")
    expected = {
        "owner": (True, True, True),
        "admin": (True, True, True),
        "ws_admin": (True, True, True),
        "editor": (True, True, False),
        "viewer": (False, False, False),
    }
    for name, (can_upload, write, manage) in expected.items():
        res = world.api.get(world.path("/documents"), world.everyone[name])
        assert res.body["can_upload"] is can_upload, name
        assert res.body["items"][0]["permissions"] == {"write": write, "manage": manage}, name
        assert res.body["items"][0]["id"] == str(doc.id)


def test_member_without_role_or_grants_and_outsiders_are_denied(world: World) -> None:
    world.seed(title="Secret")
    res = world.api.get(world.path("/documents"), world.plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    res = world.api.get(world.path("/documents"), world.t.outsider)
    assert_denied(world.api, res, 404, action="document.read", reason="not_org_member")
    # Unauthenticated.
    res = world.api.get(world.path("/documents"), None)
    assert res.status == 401


def test_ids_from_other_orgs_are_404_everywhere(world: World) -> None:
    world.seed(title="Mine")
    # Our workspace id under the foreign org (the owner is not a member of it).
    res = world.api.get(
        f"/v1/orgs/{world.foreign.org_id}/workspaces/{world.ws}/documents", world.t.owner
    )
    assert res.status == 404
    # The foreign workspace under our org.
    res = world.api.get(
        f"/v1/orgs/{world.org_id}/workspaces/{world.foreign_ws}/documents", world.t.owner
    )
    assert res.status == 404
    assert res.body == {"detail": "not_found"}
    # Random ids.
    res = world.api.get(
        f"/v1/orgs/{uuid.uuid4()}/workspaces/{uuid.uuid4()}/documents", world.t.owner
    )
    assert res.status == 404


def test_grants_give_visibility_without_a_role(world: World) -> None:
    read = world.seed(title="read-grant")
    write = world.seed(title="write-grant")
    manage = world.seed(title="manage-grant")
    expired = world.seed(title="expired-grant")
    revoked = world.seed(title="revoked-grant")
    role_grant = world.seed(title="role-grant")
    world.seed(title="no-grant")
    user = world.plain
    world.grant(read.id, user, "read")
    world.grant(write.id, user, "write")
    world.grant(manage.id, user, "manage")
    world.grant(expired.id, user, "read", expires_in="-1 hour")
    world.grant(revoked.id, user, "read", revoked=True)
    # A grant to a workspace role means nothing for a user who has no workspace role.
    world.grant(role_grant.id, user, "read", grantee_type="workspace_role", grantee_id="viewer")
    # Someone else's grant is not mine.
    world.grant(expired.id, world.other_plain, "read")

    res = world.api.get(world.path("/documents"), user, params={"sort": "title", "order": "asc"})
    assert res.status == 200, res.body
    assert world.titles(res) == ["manage-grant", "read-grant", "write-grant"]
    perms = {i["title"]: i["permissions"] for i in res.body["items"]}
    assert perms == {
        "read-grant": {"write": False, "manage": False},
        "write-grant": {"write": True, "manage": False},
        "manage-grant": {"write": True, "manage": True},
    }
    assert res.body["can_upload"] is False  # creating needs a workspace role

    seen = world.api.get(world.path("/documents"), world.other_plain)
    assert world.titles(seen) == ["expired-grant"]


def test_only_expired_or_revoked_grants_mean_no_access(world: World) -> None:
    doc = world.seed(title="gone")
    world.grant(doc.id, world.plain, "manage", expires_in="-1 minute")
    world.grant(doc.id, world.plain, "write", revoked=True)
    res = world.api.get(world.path("/documents"), world.plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")


def test_grant_on_a_deleted_document_gives_nothing(world: World) -> None:
    doc = world.seed(title="deleted", deleted=True)
    world.grant(doc.id, world.plain, "read")
    res = world.api.get(world.path("/documents"), world.plain)
    assert res.status == 404


def test_workspace_role_grant_adds_permissions_for_role_holders(world: World) -> None:
    doc = world.seed(title="viewers-can-write")
    world.grant(doc.id, world.viewer, "write", grantee_type="workspace_role", grantee_id="viewer")
    res = world.api.get(world.path("/documents"), world.viewer)
    assert res.body["items"][0]["permissions"] == {"write": True, "manage": False}
    # The editor does not hold the 'viewer' role grant but is an editor anyway.
    res = world.api.get(world.path("/documents"), world.editor)
    assert res.body["items"][0]["permissions"] == {"write": True, "manage": False}


# --- filters --------------------------------------------------------------------------------


@pytest.fixture
def library(world: World) -> World:
    world.seed(title="Quarterly Report", filename="summary.pdf", content_type="application/pdf")
    world.seed(title="100% Done", filename="a.txt", content_type="text/plain")
    world.seed(title="snake_case", filename="b.md", content_type="text/markdown")
    world.seed(title="snakeXcase", filename="c.png", content_type="image/png")
    world.seed(title="back\\slash", filename="d.txt", content_type="text/plain")
    world.seed(title="Alpha", filename="Budget-2024.XLSX", content_type="application/x-xlsx")
    world.seed(title="Old name", filename="current.pdf", content_type="application/pdf", versions=2)
    return world


def listing(world: World, **params: Any) -> list[str]:
    params.setdefault("sort", "title")
    return world.titles(world.api.get(world.path("/documents"), world.t.owner, params=params))


def test_q_is_a_case_insensitive_substring_of_title_or_latest_filename(library: World) -> None:
    assert listing(library, q="QUARTERLY") == ["Quarterly Report"]
    assert listing(library, q="arterly rep") == ["Quarterly Report"]
    assert listing(library, q="budget") == ["Alpha"]  # latest filename
    assert listing(library, q="summary.PDF") == ["Quarterly Report"]
    assert listing(library, q="v1-") == []  # an older version's filename does not match
    assert listing(library, q="current") == ["Old name"]
    assert listing(library, q="nothing-like-this") == []
    assert len(listing(library, q="   ")) == 7  # blank = no filter


def test_q_wildcards_match_literally(library: World) -> None:
    assert listing(library, q="%") == ["100% Done"]
    assert listing(library, q="_") == ["snake_case"]  # not snakeXcase
    assert listing(library, q="\\") == ["back\\slash"]
    assert listing(library, q="snake_c") == ["snake_case"]
    assert listing(library, q="100%") == ["100% Done"]
    assert listing(library, q="%%") == []
    assert listing(library, q="\\%") == []


def test_content_type_exact_and_prefix(library: World) -> None:
    assert listing(library, content_type="text/plain") == ["100% Done", "back\\slash"]
    assert listing(library, content_type="text/") == ["100% Done", "back\\slash", "snake_case"]
    assert listing(library, content_type="image/") == ["snakeXcase"]
    assert listing(library, content_type="TEXT/PLAIN") == ["100% Done", "back\\slash"]
    assert listing(library, content_type="text") == []  # no slash: exact match only
    assert listing(library, content_type="application/pdf") == ["Old name", "Quarterly Report"]
    assert listing(library, content_type="te%") == []
    assert listing(library, content_type="application/") == [
        "Alpha",
        "Old name",
        "Quarterly Report",
    ]


def test_filters_combine(library: World) -> None:
    assert listing(library, q="txt", content_type="text/") == ["100% Done", "back\\slash"]
    assert listing(library, q="snake", content_type="text/") == ["snake_case"]
    assert listing(library, q="snake", content_type="image/png") == ["snakeXcase"]
    assert listing(library, q="snake", content_type="application/") == []


def test_filters_apply_to_what_a_grant_holder_can_see(library: World) -> None:
    res = library.api.get(library.path("/documents"), library.t.owner, params={"q": "alpha"})
    doc_id = uuid.UUID(res.body["items"][0]["id"])
    library.grant(doc_id, library.plain, "read")
    res = library.api.get(library.path("/documents"), library.plain, params={"q": "alpha"})
    assert library.titles(res) == ["Alpha"]
    res = library.api.get(library.path("/documents"), library.plain, params={"q": "snake"})
    assert library.titles(res) == []  # a visible workspace, but nothing matching


# --- sorting and pagination -----------------------------------------------------------------


@pytest.fixture
def ordered(world: World) -> World:
    """Seven documents with ties on ``updated_at`` and on ``title`` (ties broken by id)."""
    rows = [
        ("alpha", 0, 0),
        ("bravo", 1, 0),
        ("bravo", 2, 1),
        ("bravo", 3, 1),
        ("charlie", 4, 2),
        ("delta", 5, 2),
        ("echo", 6, 3),
    ]
    for title, created_hours, updated_step in rows:
        world.seed(
            title=title,
            created_at=T0 + timedelta(hours=created_hours),
            updated_at=T0 + timedelta(hours=10 + updated_step),
        )
    return world


def expected_order(world: World, sort: str, order: str) -> list[str]:
    with world.engine.connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(
            text(
                "SELECT id, title, created_at, updated_at FROM documents"
                " WHERE workspace_id = :w AND deleted_at IS NULL"
            ),
            {"w": world.ws},
        ).all()
    keyed = sorted(rows, key=lambda r: (getattr(r, sort), r.id), reverse=order == "desc")
    return [str(r.id) for r in keyed]


def walk(world: World, user: Any, limit: int, **params: Any) -> tuple[list[str], int]:
    seen: list[str] = []
    pages = 0
    cursor: str | None = None
    while True:
        query = {**params, "limit": limit}
        if cursor:
            query["cursor"] = cursor
        res = world.api.get(world.path("/documents"), user, params=query)
        assert res.status == 200, res.body
        pages += 1
        seen.extend(item["id"] for item in res.body["items"])
        cursor = res.body["next_cursor"]
        if cursor is None:
            return seen, pages
        assert len(res.body["items"]) == limit
        assert pages < 20


@pytest.mark.parametrize("sort", ["updated_at", "created_at", "title"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_cursor_walk_has_no_gaps_or_duplicates(ordered: World, sort: str, order: str) -> None:
    for limit in (2, 3):
        seen, pages = walk(ordered, ordered.t.owner, limit, sort=sort, order=order)
        assert pages >= 3
        assert len(seen) == len(set(seen)) == 7
        assert seen == expected_order(ordered, sort, order), (sort, order, limit)


def test_default_sort_and_order(ordered: World) -> None:
    res = ordered.api.get(ordered.path("/documents"), ordered.t.owner)
    assert ids(res) == expected_order(ordered, "updated_at", "desc")
    res = ordered.api.get(ordered.path("/documents"), ordered.t.owner, params={"sort": "title"})
    assert ids(res) == expected_order(ordered, "title", "asc")
    res = ordered.api.get(
        ordered.path("/documents"), ordered.t.owner, params={"sort": "created_at"}
    )
    assert ids(res) == expected_order(ordered, "created_at", "desc")


def test_cursor_walk_with_filters_and_grants(ordered: World) -> None:
    with ordered.engine.connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(
            text("SELECT id FROM documents WHERE workspace_id = :w AND title = 'bravo'"),
            {"w": ordered.ws},
        ).all()
    for row in rows:
        ordered.grant(row.id, ordered.plain, "read")
    seen, pages = walk(ordered, ordered.plain, 1, sort="title", q="bra")
    assert pages == 3
    assert sorted(seen) == sorted(str(r.id) for r in rows)
    assert seen == sorted(seen)  # equal titles: ascending id


def test_limit_bounds(world: World) -> None:
    world.seed(title="x")
    for bad in (0, -1, 201, "abc"):
        res = world.api.get(world.path("/documents"), world.t.owner, params={"limit": bad})
        assert res.status == 422, bad
        assert res.body["detail"] == "invalid_request"
    for good in (1, 200):
        res = world.api.get(world.path("/documents"), world.t.owner, params={"limit": good})
        assert res.status == 200


def test_invalid_sort_and_order_are_422(world: World) -> None:
    for params in ({"sort": "size"}, {"order": "sideways"}, {"sort": ""}):
        res = world.api.get(world.path("/documents"), world.t.owner, params=params)
        assert res.status == 422
        assert res.body["detail"] == "invalid_request"


def _token(*parts: str) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(parts)).encode()).decode().rstrip("=")


def test_invalid_cursors(ordered: World) -> None:
    first = ordered.api.get(
        ordered.path("/documents"), ordered.t.owner, params={"limit": 2, "sort": "title"}
    )
    cursor = first.body["next_cursor"]
    assert cursor
    doc_id = str(uuid.uuid4())
    garbage = [
        "not-a-cursor",
        "!!!",
        "%%%%",
        _token("documents:title:asc", "x"),  # wrong arity
        _token("documents:updated_at:desc", "not-a-date", doc_id),
        _token("documents:updated_at:desc", "2026-01-01T00:00:00", doc_id),  # naive timestamp
        _token("documents:updated_at:desc", "2026-01-01T00:00:00+00:00", "not-a-uuid"),
        base64.urlsafe_b64encode(b"[1,2,3]").decode(),
        base64.urlsafe_b64encode(b"{}").decode(),
    ]
    for bad in garbage:
        res = ordered.api.get(ordered.path("/documents"), ordered.t.owner, params={"cursor": bad})
        assert (res.status, res.body) == (422, {"detail": "invalid_cursor"}), bad
    # A cursor is only valid for the sort and the order it was issued for.
    for params in (
        {"sort": "updated_at"},
        {"sort": "created_at", "order": "asc"},
        {"sort": "title", "order": "desc"},
    ):
        res = ordered.api.get(
            ordered.path("/documents"), ordered.t.owner, params={**params, "cursor": cursor}
        )
        assert (res.status, res.body) == (422, {"detail": "invalid_cursor"}), params
    ok = ordered.api.get(
        ordered.path("/documents"),
        ordered.t.owner,
        params={"sort": "title", "order": "asc", "cursor": cursor},
    )
    assert ok.status == 200


def test_cursor_of_the_versions_endpoint_is_not_a_document_cursor(world: World) -> None:
    doc = world.seed(title="v", versions=3)
    page = world.api.get(world.doc_path(doc.id, "/versions"), world.t.owner, params={"limit": 1})
    res = world.api.get(
        world.path("/documents"), world.t.owner, params={"cursor": page.body["next_cursor"]}
    )
    assert (res.status, res.body) == (422, {"detail": "invalid_cursor"})


def test_a_cursor_does_not_leak_documents_of_other_workspaces(world: World) -> None:
    world.seed(title="a")
    world.seed(title="b")
    world.seed_foreign(title="foreign")
    first = world.api.get(world.path("/documents"), world.t.owner, params={"limit": 1})
    second = world.api.get(
        world.path("/documents"),
        world.t.owner,
        params={"limit": 5, "cursor": first.body["next_cursor"]},
    )
    assert "foreign" not in world.titles(second)
    assert len(second.body["items"]) == 1


def test_unicode_titles_round_trip_through_the_cursor(world: World) -> None:
    for title in ("Zoë ☃ 1", "Zoë ☃ 2", "Zoë ☃ 3", "日本語 4"):
        world.seed(title=title)
    seen, pages = walk(world, world.t.owner, 1, sort="title")
    assert pages == 4
    assert len(set(seen)) == 4


# --- N+1 -------------------------------------------------------------------------------------


class QueryCounter:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        self.statements.append(statement)

    def __enter__(self) -> QueryCounter:
        event.listen(SAEngine, "before_cursor_execute", self)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(SAEngine, "before_cursor_execute", self)


def test_list_query_count_does_not_grow_with_the_page(world: World) -> None:
    for i in range(50):
        doc = world.seed(title=f"doc {i:02d}", versions=1 + i % 3)
        if i % 2:
            world.grant(doc.id, world.viewer, "write")
    counts: dict[str, dict[int, int]] = {}
    for name, user in (("owner", world.t.owner), ("viewer", world.viewer)):
        counts[name] = {}
        for limit in (5, 50):
            with QueryCounter() as counter:
                res = world.api.get(world.path("/documents"), user, params={"limit": limit})
            assert res.status == 200
            assert len(res.body["items"]) == limit
            counts[name][limit] = len(counter.statements)
        assert counts[name][5] == counts[name][50], counts
        assert counts[name][50] <= 12, (name, counts)
    # A user with no role sees only granted documents; still constant.
    for doc_row in world.api.get(
        world.path("/documents"), world.t.owner, params={"limit": 50}
    ).body["items"]:
        world.grant(uuid.UUID(doc_row["id"]), world.plain, "read")
    with QueryCounter() as counter:
        res = world.api.get(world.path("/documents"), world.plain, params={"limit": 50})
    assert len(res.body["items"]) == 50
    assert len(counter.statements) <= 12, counter.statements
