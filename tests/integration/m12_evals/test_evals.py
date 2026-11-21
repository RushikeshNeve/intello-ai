"""M12.03 workspace golden sets: CRUD, a real run against the workspace-scoped retrieval service,
concealment of other workspaces / orgs, and the failure paths."""

from __future__ import annotations

import uuid
from typing import Any

from catalog_support import World
from ret_support import Rig

TEXT_1 = ["Kestrel telemetry dashboard guide for the first workspace."]
TEXT_2 = ["Kestrel telemetry dashboard notes for the second workspace."]
QUESTION = "kestrel telemetry dashboard"


def base(world: World, ws: uuid.UUID | None = None) -> str:
    return f"/v1/orgs/{world.org_id}/workspaces/{ws or world.ws}/evals"


def new_set(world: World, name: str = "golden") -> dict[str, Any]:
    res = world.api.post(f"{base(world)}/sets", world.ws_admin, {"name": name})
    assert res.status == 201, res.body
    return res.body  # type: ignore[no-any-return]


def test_crud_and_run_is_confined_to_the_workspace(rig: Rig, world: World) -> None:
    api = world.api
    ws2 = api.create_workspace(world.org_id, world.t.owner)
    d1 = rig.seed_world(world, title="One")
    d2 = rig.seed(world.org_id, ws2, world.t.owner, title="Two")
    rig.publish(world.org_id, world.ws, d1, TEXT_1)
    rig.publish(world.org_id, ws2, d2, TEXT_2)

    s = new_set(world)
    assert (s["version"], s["case_count"]) == (1, 0)
    cases = f"{base(world)}/sets/{s['id']}/cases"
    hit = api.post(
        cases,
        world.ws_admin,
        {"question": QUESTION, "expected_document_ids": [str(d1.id)], "notes": "n"},
    )
    assert hit.status == 201, hit.body
    miss = api.post(cases, world.ws_admin, {"question": "xylophone quasar", "should_abstain": True})
    assert miss.status == 201
    # edit + rename bump the version; delete works
    patched = api.patch(f"{cases}/{miss.body['id']}", world.ws_admin, {"notes": "edited"})
    assert patched.status == 200
    assert patched.body["notes"] == "edited"
    assert (
        api.patch(f"{base(world)}/sets/{s['id']}", world.ws_admin, {"name": "gold2"}).status == 200
    )
    got = api.get(f"{base(world)}/sets/{s['id']}", world.ws_admin).body
    assert (got["name"], got["case_count"], got["version"]) == ("gold2", 2, 5)

    # the org owner reads BOTH workspaces, yet the run only ever sees this one
    run = api.post(
        f"{base(world)}/sets/{s['id']}/runs", world.t.owner, {"mode": "keyword", "top_k": 5}
    )
    assert run.status == 201, run.body
    body = run.body
    assert body["state"] == "completed"
    assert body["set_version"] == 5
    assert body["config"]["top_k"] == 5
    assert body["summary"]["hit_at_k"] == 1.0
    assert body["summary"]["abstention_accuracy"] == 1.0
    by_q = {r["question"]: r for r in body["results"]}
    found = {x["document_id"] for r in body["results"] for x in r["retrieved"]}
    assert found == {str(d1.id)}
    assert str(d2.id) not in found
    assert by_q[QUESTION]["metrics"]["rank"] == 1
    assert by_q["xylophone quasar"]["answer_status"] == "abstained"

    again = api.get(f"{base(world)}/sets/{s['id']}/runs/{body['id']}", world.ws_admin)
    assert again.status == 200
    assert len(again.body["results"]) == 2
    listed = api.get(f"{base(world)}/sets/{s['id']}/runs", world.ws_admin).body["items"]
    assert [r["id"] for r in listed] == [body["id"]]
    assert api.delete(f"{cases}/{hit.body['id']}", world.ws_admin).status == 204
    assert api.delete(f"{base(world)}/sets/{s['id']}", world.ws_admin).status == 204
    assert api.get(f"{base(world)}/sets/{s['id']}", world.ws_admin).status == 404


def test_only_workspace_admins_and_other_scopes_are_concealed(rig: Rig, world: World) -> None:
    api = world.api
    s = new_set(world)
    sets = f"{base(world)}/sets"
    for user in (world.viewer, world.editor):  # members without admin: forbidden
        assert api.get(sets, user).status == 403
        assert api.post(sets, user, {"name": "x"}).status == 403
    for user in (world.plain, world.t.outsider, world.foreign.owner):  # concealed
        assert api.get(sets, user).status == 404
        assert api.post(f"{sets}/{s['id']}/runs", user, {}).status == 404
        assert api.get(f"{sets}/{s['id']}", user).status == 404
    # a set is invisible through another workspace's path
    ws2 = api.create_workspace(world.org_id, world.t.owner)
    assert api.get(f"{base(world, ws2)}/sets/{s['id']}", world.t.owner).status == 404
    assert api.get(f"{base(world, ws2)}/sets", world.t.owner).body["items"] == []
    # and never through another org's path
    foreign = f"/v1/orgs/{world.foreign.org_id}/workspaces/{world.foreign_ws}/evals/sets"
    assert api.get(f"{foreign}/{s['id']}", world.foreign.owner).status == 404


def test_run_failure_paths(rig: Rig, world: World) -> None:
    api = world.api
    s = new_set(world)
    runs = f"{base(world)}/sets/{s['id']}/runs"
    assert api.post(runs, world.ws_admin, {"mode": "keyword"}).status == 422  # empty set
    assert api.post(runs, world.ws_admin, {"top_k": 0}).status == 422  # invalid body
    unknown = f"{base(world)}/sets/{uuid.uuid4()}/runs"
    assert api.post(unknown, world.ws_admin, {}).status == 404
