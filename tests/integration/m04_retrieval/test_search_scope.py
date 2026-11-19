"""M04.01.01 / M04.01.02: the scope is derived from PostgreSQL per request; nothing outside it is
ever returned, even when it is the best match (both indexes, real RLS)."""

from __future__ import annotations

import uuid

from catalog_support import World
from ret_support import Rig, doc_ids, search
from sqlalchemy import text

from app.modules.retrieval import EmptyScope, RetrievalScope, resolve_scope

QUERY = "kestrel telemetry dashboard"
TEXT_1 = ["Kestrel telemetry dashboard guide for the first workspace."]
TEXT_2 = ["Kestrel telemetry dashboard notes for the second workspace."]


def second_workspace(world: World) -> uuid.UUID:
    return world.api.create_workspace(world.org_id, world.t.owner)


def seed_two(rig: Rig, world: World) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(ws2, doc in ws1, doc in ws2), both published and searchable."""
    ws2 = second_workspace(world)
    d1 = rig.seed_world(world, title="One")
    d2 = rig.seed(world.org_id, ws2, world.t.owner, title="Two")
    rig.publish(world.org_id, world.ws, d1, TEXT_1)
    rig.publish(world.org_id, ws2, d2, TEXT_2)
    return ws2, d1.id, d2.id


def test_each_role_sees_only_its_own_workspaces(rig: Rig, world: World) -> None:
    ws2, d1, d2 = seed_two(rig, world)
    only2 = world.api.user("ws2only")
    world.api.set_role(world.org_id, world.t.owner, only2, "member")
    res = world.api.put(
        f"/v1/orgs/{world.org_id}/workspaces/{ws2}/members/{only2.id}",
        world.t.owner,
        {"role": "editor"},
    )
    assert res.status == 200
    for user in (world.viewer, world.editor, world.ws_admin):
        found = search(world, user, query=QUERY)
        assert doc_ids(found) == [str(d1)]
        assert found.body["scope"] == {"workspace_count": 1}
    assert doc_ids(search(world, only2, query=QUERY)) == [str(d2)]
    for manager in (world.t.owner, world.t.admin):  # org owner/admin read every workspace
        found = search(world, manager, query=QUERY)
        assert set(doc_ids(found)) == {str(d1), str(d2)}
        assert found.body["scope"] == {"workspace_count": 2}


def test_member_without_workspace_role_and_outsider(rig: Rig, world: World) -> None:
    seed_two(rig, world)
    plain = search(world, world.plain, query=QUERY)
    assert plain.status == 200
    assert plain.body["results"] == []
    assert plain.body["scope"] == {"workspace_count": 0}
    assert search(world, world.t.outsider, query=QUERY).status == 404  # concealed
    assert search(world, None, query=QUERY).status == 401
    denied = rig.q(
        "SELECT count(*) FROM audit_events WHERE org_id = :o AND event_type = 'authz.denied'"
        " AND actor_user_id = :u",
        o=world.org_id,
        u=world.t.outsider.id,
    )
    assert denied[0][0] == 1  # the concealed 404 is audited like every other denial


def test_removed_membership_is_effective_on_the_next_request(rig: Rig, world: World) -> None:
    seed_two(rig, world)
    assert len(search(world, world.viewer, query=QUERY).body["results"]) == 1
    res = world.api.delete(
        f"/v1/orgs/{world.org_id}/workspaces/{world.ws}/members/{world.viewer.id}",
        world.t.owner,
    )
    assert res.status == 204
    after = search(world, world.viewer, query=QUERY)
    assert after.status == 200
    assert after.body["results"] == []
    res = world.api.delete(f"/v1/orgs/{world.org_id}/members/{world.editor.id}", world.t.owner)
    assert res.status == 204
    assert search(world, world.editor, query=QUERY).status == 404


def test_soft_deleted_workspace_is_excluded(rig: Rig, world: World) -> None:
    ws2, d1, _d2 = seed_two(rig, world)
    with rig.admin.begin() as conn:
        conn.execute(text("UPDATE workspaces SET deleted_at = now() WHERE id = :w"), {"w": ws2})
    found = search(world, world.t.owner, query=QUERY)
    assert doc_ids(found) == [str(d1)]
    assert found.body["scope"] == {"workspace_count": 1}


def test_cross_workspace_best_match_is_never_returned(rig: Rig, world: World) -> None:
    ws2 = second_workspace(world)
    mine = rig.seed_world(world, title="Mine")
    theirs = rig.seed(world.org_id, ws2, world.t.owner, title="Theirs")
    rig.publish(world.org_id, world.ws, mine, ["A passing mention of kestrel only."])
    rig.publish(  # the best possible match, but in a workspace the viewer cannot read
        world.org_id, ws2, theirs, ["Kestrel telemetry dashboard kestrel telemetry dashboard."]
    )
    found = search(world, world.viewer, query=QUERY)
    assert doc_ids(found) == [str(mine.id)]
    assert str(theirs.id) not in str(found.body)
    both = search(world, world.t.owner, query=QUERY)
    assert doc_ids(both)[0] == str(theirs.id)  # and the owner does get the best match first


def test_cross_org_is_never_returned(rig: Rig, world: World) -> None:
    mine = rig.seed_world(world, title="Mine")
    rig.publish(world.org_id, world.ws, mine, ["Kestrel telemetry dashboard of org A."])
    foreign = rig.seed(world.foreign.org_id, world.foreign_ws, world.foreign.owner, title="Foreign")
    rig.publish(
        world.foreign.org_id,
        world.foreign_ws,
        foreign,
        ["Kestrel telemetry dashboard kestrel telemetry dashboard of org B."],
    )
    found = search(world, world.t.owner, query=QUERY)
    assert doc_ids(found) == [str(mine.id)]
    assert str(foreign.id) not in str(found.body)
    # The foreign owner cannot use this org's id, and sees only its own data in its own org.
    assert (
        world.api.post(
            f"/v1/orgs/{world.org_id}/search", world.foreign.owner, {"query": QUERY}
        ).status
        == 404
    )
    own = world.api.post(
        f"/v1/orgs/{world.foreign.org_id}/search", world.foreign.owner, {"query": QUERY}
    )
    assert doc_ids(own) == [str(foreign.id)]


def test_client_supplied_workspace_ids_only_narrow(rig: Rig, world: World) -> None:
    ws2, d1, d2 = seed_two(rig, world)
    forged = [str(ws2), str(world.foreign_ws), str(uuid.uuid4())]
    found = search(world, world.viewer, query=QUERY, workspace_ids=[*forged, str(world.ws)])
    assert doc_ids(found) == [str(d1)]
    only_forged = search(world, world.viewer, query=QUERY, workspace_ids=forged)
    assert only_forged.status == 200  # ignored, never an error that leaks existence
    assert only_forged.body["results"] == []
    owner_narrow = search(world, world.t.owner, query=QUERY, workspace_ids=[str(ws2)])
    assert doc_ids(owner_narrow) == [str(d2)]


def test_grants_do_not_widen_search_scope(rig: Rig, world: World) -> None:
    seed = rig.seed_world(world, title="Granted")
    rig.publish(world.org_id, world.ws, seed, TEXT_1)
    world.grant(seed.id, world.plain, "read")
    found = search(world, world.plain, query=QUERY)
    assert found.status == 200
    assert found.body["results"] == []  # DC-025: grants open one document, they never enable search


def test_resolve_scope_directly_matches_the_api_rules(rig: Rig, world: World) -> None:
    ws2 = second_workspace(world)
    with rig.tenant(world.viewer, world.org_id) as session:
        scope = resolve_scope(session, org_id=world.org_id, user_id=world.viewer.id)
        assert isinstance(scope, RetrievalScope)
        assert scope.workspace_ids == frozenset({world.ws})
        assert ws2 not in scope.workspace_ids
        assert scope.filters().workspace_ids == scope.workspace_ids
        try:
            resolve_scope(
                session,
                org_id=world.org_id,
                user_id=world.viewer.id,
                requested_workspace_ids=[ws2],
            )
            raise AssertionError("expected EmptyScope")
        except EmptyScope:
            pass
    with rig.tenant(world.t.owner, world.org_id) as session:
        both = resolve_scope(session, org_id=world.org_id, user_id=world.t.owner.id)
        assert both.workspace_ids == frozenset({world.ws, ws2})
