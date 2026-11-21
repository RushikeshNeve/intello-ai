"""M08.01.01: tenant and grant adversarial sweep over the LIVE OpenAPI schema of ``create_app()``.

For EVERY route that has an ``{org_id}`` path parameter:

* (a) no credentials -> 401;
* (b) a user of ANOTHER org -> 404 ``not_found`` (existence concealed, never 403/200);
* (c) an org member WITHOUT access to the workspace -> 404 (workspace routes);
* (d) a workspace viewer (or a plain org member on org-level admin routes) on a WRITE route -> 403.

Per-route fixtures come from ``BODIES`` (request bodies) and the ids in ``ids()``; unknown path
params get random UUIDs. Routes that cannot be exercised generically are in ``SKIP`` with a reason;
a guard test fails when a new route is neither swept nor skipped. Also: a document grant opens ONE
document only (not search/graph/lists/counts), and no count or title of an unauthorized item leaks.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from catalog_support import World
from ret_support import Rig
from sqlalchemy import text
from sweep_support import WRITE, fill, tenant_routes

from app.main import create_app

ROUTES = tenant_routes(create_app())

# (METHOD, template) -> reason it cannot be swept generically.
SKIP: dict[tuple[str, str], str] = {
    ("POST", "/v1/orgs"): "no {org_id}: org creation is by definition open to any signed-in user",
}

# Request bodies for write routes (validation must pass so authorization is what answers).
BODIES: dict[tuple[str, str], Any] = {
    ("PATCH", "/v1/orgs/{org_id}"): {"name": "x"},
    ("PUT", "/v1/orgs/{org_id}/members/{user_id}"): {"role": "member"},
    ("POST", "/v1/orgs/{org_id}/workspaces"): {"slug": "sweep-ws", "name": "Sweep"},
    ("PATCH", "/v1/orgs/{org_id}/workspaces/{workspace_id}"): {"name": "x"},
    ("PUT", "/v1/orgs/{org_id}/workspaces/{workspace_id}/members/{user_id}"): {"role": "viewer"},
    ("POST", "/v1/orgs/{org_id}/workspaces/{workspace_id}/uploads"): {
        "filename": "a.txt",
        "content_type": "text/plain",
        "size_bytes": 5,
        "sha256": "a" * 64,
    },
    ("POST", "/v1/orgs/{org_id}/workspaces/{workspace_id}/documents/from-url"): {
        "url": "https://example.com/a.txt"
    },
    ("POST", "/v1/orgs/{org_id}/workspaces/{workspace_id}/sources"): {
        "provider": "github",
        "repo": "acme/docs",
    },
    ("PUT", "/v1/orgs/{org_id}/ai-policy"): {
        "allow_external": False,
        "allowed_providers": ["local"],
    },
    ("PATCH", "/v1/orgs/{org_id}/chat/threads/{thread_id}"): {"title": "x"},
    ("POST", "/v1/orgs/{org_id}/chat/threads/{thread_id}/runs"): {
        "content": "hello",
        "idempotency_key": "sweep-key-0001",
    },
    ("POST", "/v1/orgs/{org_id}/memory/suggestions"): {"statement": "s", "kind": "fact"},
    ("POST", "/v1/orgs/{org_id}/research"): {"question": "what?"},
    ("PATCH", "/v1/orgs/{org_id}/workspaces/{workspace_id}/documents/{document_id}"): {
        "title": "x"
    },
    (
        "POST",
        "/v1/orgs/{org_id}/workspaces/{workspace_id}/graph/merge-candidates/{candidate_id}/decision",
    ): {"decision": "reject"},
    ("POST", "/v1/orgs/{org_id}/search"): {"query": "anything"},
    ("POST", "/v1/orgs/{org_id}/members/preapprovals"): {
        "email": "sweep@example.com",
        "role": "member",
    },
    # M12.01 prompt registry, M12.03 evals.
    ("POST", "/v1/orgs/{org_id}/prompts/{kind}/versions"): {"body": "Answer briefly."},
    ("POST", "/v1/orgs/{org_id}/workspaces/{workspace_id}/evals/sets"): {"name": "Sweep"},
    ("PATCH", "/v1/orgs/{org_id}/workspaces/{workspace_id}/evals/sets/{set_id}"): {"name": "x"},
    ("POST", "/v1/orgs/{org_id}/workspaces/{workspace_id}/evals/sets/{set_id}/cases"): {
        "question": "what?"
    },
}

# Org-level routes only org owners/admins may WRITE: a plain org member gets 403 (d').
ORG_ADMIN_WRITES = {
    ("PATCH", "/v1/orgs/{org_id}"),
    ("DELETE", "/v1/orgs/{org_id}"),
    ("PUT", "/v1/orgs/{org_id}/members/{user_id}"),
    ("DELETE", "/v1/orgs/{org_id}/members/{user_id}"),
    ("POST", "/v1/orgs/{org_id}/workspaces"),
    ("PUT", "/v1/orgs/{org_id}/ai-policy"),
}
# Workspace "writes" that any workspace member may legitimately call (reads shaped as POST).
VIEWER_MAY: set[tuple[str, str]] = set()


def _ids(world: World, rig: Rig) -> dict[str, Any]:
    doc = rig.seed_world(world, title="Sweep doc")
    pub = rig.publish(world.org_id, world.ws, doc, ["sweep text"])
    return {
        "org_id": world.org_id,
        "workspace_id": world.ws,
        "document_id": doc.id,
        "version_id": doc.versions[0],
        "chunk_id": pub.chunk_ids[0],
        "user_id": world.plain.id,
        "kind": "chat_system",  # M12.01 prompt kinds are an enum, not a UUID
    }


def _call(world: World, method: str, tpl: str, ids: dict[str, Any], user: Any) -> Any:
    body = BODIES.get((method, tpl), {} if method in WRITE else None)
    params = {"entity_id": str(uuid.uuid4())} if tpl.endswith("/graph/neighborhood") else None
    return world.api.call(method, fill(tpl, ids), user, json=body, params=params)


def test_every_tenant_route_is_swept_or_skipped() -> None:
    assert len(ROUTES) >= 55, "the live OpenAPI schema shrank: the sweep would be vacuous"
    assert all(k in {(m, p) for m, p in ROUTES} or "{org_id}" not in k[1] for k in SKIP)
    assert not [r for r in ROUTES if "{org_id}" not in r[1]]


@pytest.mark.parametrize(("method", "tpl"), ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
def test_route_is_tenant_scoped(rig: Rig, world: World, method: str, tpl: str) -> None:
    if (method, tpl) in SKIP:
        pytest.skip(SKIP[(method, tpl)])
    ids = _ids(world, rig)
    # (a) unauthenticated
    assert _call(world, method, tpl, ids, None).status == 401
    # (b) a user of another org: concealed
    res = _call(world, method, tpl, ids, world.foreign.owner)
    assert (res.status, res.body) == (404, {"detail": "not_found"}), (res.status, res.body)
    if "{workspace_id}" in tpl:
        # (c) org member without workspace access: concealed
        res = _call(world, method, tpl, ids, world.plain)
        assert (res.status, res.body) == (404, {"detail": "not_found"}), (res.status, res.body)
        # (d) viewer on a write route
        if method in WRITE and (method, tpl) not in VIEWER_MAY:
            res = _call(world, method, tpl, ids, world.viewer)
            assert res.status == 403, (res.status, res.body)
    elif (method, tpl) in ORG_ADMIN_WRITES:
        res = _call(world, method, tpl, ids, world.plain)
        assert res.status == 403, (res.status, res.body)


# ------------------------------------------------------------------------------------ grants


def _grant_read(world: World, document_id: uuid.UUID, user_id: uuid.UUID) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type, resource_id,"
                " grantee_type, grantee_id, permission, granted_by)"
                " VALUES (:id, :o, :w, 'document', :r, 'user', :g, 'read', :by)"
            ),
            {
                "id": uuid.uuid4(),
                "o": world.org_id,
                "w": world.ws,
                "r": document_id,
                "g": str(user_id),
                "by": world.t.owner.id,
            },
        )


def test_document_grant_opens_exactly_one_document(rig: Rig, world: World) -> None:
    api, org, ws, user = world.api, world.org_id, world.ws, world.plain
    a, b = rig.seed_world(world, title="Granted"), rig.seed_world(world, title="Hidden")
    pa = rig.publish(org, ws, a, ["alpha granted passage zebra"])
    pb = rig.publish(org, ws, b, ["bravo hidden passage zebra"])
    assert api.get(world.doc_path(a.id), user).status == 404  # before the grant
    _grant_read(world, a.id, user.id)

    assert api.get(world.doc_path(a.id), user).status == 200
    assert api.get(world.doc_path(b.id), user).status == 404
    chunk = world.doc_path(a.id, f"/versions/{a.versions[0]}/chunks/{pa.chunk_ids[0]}")
    assert api.get(chunk, user).status == 200
    other = world.doc_path(b.id, f"/versions/{b.versions[0]}/chunks/{pb.chunk_ids[0]}")
    assert api.get(other, user).status == 404
    # the grant does not widen the workspace: no list, no graph, no workspace, no writes
    listing = api.get(world.path("/documents"), user)  # lists ONLY the granted document (by design)
    assert listing.status == 200
    assert [i["id"] for i in listing.body["items"]] == [str(a.id)]
    assert "Hidden" not in str(listing.body)
    for sub in ("/graph/entities", "/ingestion/events", ""):
        assert api.get(world.path(sub), user).status == 404, sub
    assert api.delete(world.doc_path(a.id), user).status in (403, 404)
    # ... and not search: the granted document's text and title never come back, no counts either
    found = api.post(f"/v1/orgs/{org}/search", user, {"query": "zebra passage"})
    assert found.status == 200
    assert found.body["results"] == []
    blob = str(found.body)
    for secret in ("granted passage", "hidden passage", "Granted", "Hidden", str(ws), str(a.id)):
        assert secret not in blob, secret


def test_no_counts_or_titles_of_unauthorized_workspaces_leak(rig: Rig, world: World) -> None:
    api, org = world.api, world.org_id
    ws2 = api.create_workspace(org, world.t.owner)
    secret_doc = rig.seed(org, ws2, world.t.owner, title="Quarterly layoffs plan")
    rig.publish(org, ws2, secret_doc, ["layoffs plan in the secret workspace kumquat"])
    mine = rig.seed_world(world, title="Visible doc")
    rig.publish(org, world.ws, mine, ["visible kumquat passage"])
    found = api.post(f"/v1/orgs/{org}/search", world.viewer, {"query": "kumquat"})
    assert found.status == 200
    titles = {r["title"] for r in found.body["results"]}
    assert titles == {"Visible doc"}
    assert "layoffs" not in str(found.body) and str(ws2) not in str(found.body)
    listing = api.get(f"/v1/orgs/{org}/workspaces", world.viewer)
    assert listing.status == 200
    assert str(ws2) not in str(listing.body)
    assert len(listing.body["items"] if isinstance(listing.body, dict) else listing.body) == 1
    # the unauthorized workspace answers like a missing one on every verb of interest
    assert api.get(f"/v1/orgs/{org}/workspaces/{ws2}/documents", world.viewer).status == 404
    assert api.get(f"/v1/orgs/{org}/workspaces/{ws2}/graph/entities", world.viewer).status == 404
