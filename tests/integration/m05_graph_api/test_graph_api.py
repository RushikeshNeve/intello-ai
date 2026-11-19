"""M05.03.01: graph read API happy path, pagination/caps, concealed 404, tenant isolation."""

from __future__ import annotations

import uuid
from typing import Any

from catalog_support import World
from ret_support import Rig
from sqlalchemy import text

from app.modules.graph.fake_read import FakeGraphRead
from app.modules.graph.ports_read import GClaim, GEntity, GEvidence


def _ent(org: uuid.UUID, ws: uuid.UUID, name: str) -> GEntity:
    return GEntity(uuid.uuid4(), org, ws, "concept", name, "active")


def _claim(
    org: uuid.UUID, ws: uuid.UUID, s: GEntity, o: GEntity, rev: uuid.UUID, **kw: Any
) -> GClaim:
    return GClaim(
        uuid.uuid4(), org, ws, s.id, "depends_on", o.id, None, 0.9,
        kw.get("verification", "verified"), rev, True,
    )  # fmt: skip


def _ev(c: GClaim, chunk: uuid.UUID, doc: uuid.UUID, ver: uuid.UUID) -> GEvidence:
    return GEvidence(uuid.uuid4(), c.id, chunk, c.revision_id, doc, ver, "an excerpt")


class Setup:
    def __init__(self, rig: Rig, world: World, gholder: Any) -> None:
        org, ws = world.org_id, world.ws
        self.ws2 = world.api.create_workspace(org, world.t.owner)
        d1 = rig.seed_world(world, title="Atlas notes")
        d2 = rig.seed(org, self.ws2, world.t.owner, title="Other notes")
        p1 = rig.publish(org, ws, d1, ["Atlas depends on Garnet.", "Garnet depends on Meridian."])
        p2 = rig.publish(org, self.ws2, d2, ["Zed depends on Yak."])
        self.p1 = p1
        a, b, c = (_ent(org, ws, n) for n in ("Atlas", "Garnet", "Meridian"))
        z, y = _ent(org, self.ws2, "Zed"), _ent(org, self.ws2, "Yak")
        self.a, self.b, self.c, self.z = a, b, c, z
        self.ab = _claim(org, ws, a, b, p1.revision_id)
        self.bc = _claim(org, ws, b, c, p1.revision_id)
        self.ac_unverified = _claim(org, ws, a, c, p1.revision_id, verification="unverified")
        self.zy = _claim(org, self.ws2, z, y, p2.revision_id)
        evidence = [
            _ev(self.ab, p1.chunk_ids[0], d1.id, p1.version_id),
            _ev(self.bc, p1.chunk_ids[1], d1.id, p1.version_id),
            _ev(self.ac_unverified, p1.chunk_ids[0], d1.id, p1.version_id),
            _ev(self.zy, p2.chunk_ids[0], d2.id, p2.version_id),
        ]
        gholder.graph = FakeGraphRead(
            [a, b, c, z, y], [self.ab, self.bc, self.ac_unverified, self.zy], evidence
        )
        self.world = world

    def base(self, ws: uuid.UUID | None = None) -> str:
        return f"/v1/orgs/{self.world.org_id}/workspaces/{ws or self.world.ws}/graph"


def test_happy_path_neighborhood_entities_claim(rig: Rig, world: World, gholder: Any) -> None:
    s = Setup(rig, world, gholder)
    get = world.api.get
    one = get(f"{s.base()}/neighborhood?entity_id={s.a.id}&depth=1", world.viewer)
    assert one.status == 200, one.body
    assert [e["claim_id"] for e in one.body["edges"]] == [str(s.ab.id)]  # unverified never shown
    assert one.body["edges"][0]["evidence_count"] == 1
    assert {n["name"] for n in one.body["nodes"]} == {"Atlas", "Garnet"}
    assert one.body["truncated"] is False
    assert one.body["next_cursor"] is None
    two = get(f"{s.base()}/neighborhood?entity_id={s.a.id}&depth=2", world.viewer)
    assert {n["name"] for n in two.body["nodes"]} == {"Atlas", "Garnet", "Meridian"}

    ents = get(f"{s.base()}/entities?q=gar", world.viewer)
    assert [i["name"] for i in ents.body["items"]] == ["Garnet"]
    assert "Zed" not in str(get(f"{s.base()}/entities", world.viewer).body)  # other workspace

    claim = get(f"{s.base()}/claims/{s.ab.id}", world.viewer)
    assert claim.status == 200, claim.body
    assert claim.body["claim"]["subject"]["name"] == "Atlas"
    ev = claim.body["evidence"][0]
    assert ev["chunk_id"] == str(s.p1.chunk_ids[0])
    assert ev["title"] == "Atlas notes"
    assert ev["document_id"] == str(s.p1.document_id)
    assert ev["version_id"] == str(s.p1.version_id)
    assert get(f"{s.base()}/claims/{s.ac_unverified.id}", world.viewer).status == 404


def test_pagination_and_caps(rig: Rig, world: World, gholder: Any) -> None:
    s = Setup(rig, world, gholder)
    get = world.api.get
    url = f"{s.base()}/neighborhood?entity_id={s.a.id}&depth=2&limit=1"
    first = get(url, world.viewer)
    assert len(first.body["edges"]) == 1
    assert first.body["truncated"] is True
    second = get(f"{url}&cursor={first.body['next_cursor']}", world.viewer)
    assert [e["claim_id"] for e in second.body["edges"]] == [str(s.bc.id)]
    assert second.body["next_cursor"] is None
    assert get(f"{s.base()}/neighborhood?entity_id={s.a.id}&depth=3", world.viewer).status == 422
    assert get(f"{s.base()}/neighborhood?entity_id={s.a.id}&limit=101", world.viewer).status == 422
    assert get(f"{url}&cursor=garbage", world.viewer).status == 422
    page = get(f"{s.base()}/entities?limit=1", world.viewer)
    assert len(page.body["items"]) == 1
    assert page.body["next_cursor"]
    nxt = get(f"{s.base()}/entities?limit=5&cursor={page.body['next_cursor']}", world.viewer)
    assert [i["name"] for i in nxt.body["items"]] == ["Garnet", "Meridian"]


def test_concealed_404_and_isolation(rig: Rig, world: World, gholder: Any) -> None:
    s = Setup(rig, world, gholder)
    get = world.api.get
    paths = [
        f"{s.base()}/neighborhood?entity_id={s.a.id}",
        f"{s.base()}/entities",
        f"{s.base()}/claims/{s.ab.id}",
    ]
    for p in paths:
        assert get(p, world.t.outsider).status == 404  # not a member of the org (cross-org)
        assert get(p, world.foreign.owner).status == 404
        assert get(p, world.plain).status == 404  # org member without a workspace role
    # cross-workspace: the viewer reads ws1 only; ws2 and ws2's records stay concealed
    assert get(f"{s.base(s.ws2)}/entities", world.viewer).status == 404
    assert get(f"{s.base()}/neighborhood?entity_id={s.z.id}", world.viewer).status == 404
    assert get(f"{s.base()}/claims/{s.zy.id}", world.viewer).status == 404
    # an org owner reads both workspaces, but each request stays inside its own workspace
    assert get(f"{s.base(s.ws2)}/neighborhood?entity_id={s.z.id}", world.t.owner).status == 200
    assert get(f"{s.base()}/neighborhood?entity_id={s.z.id}", world.t.owner).status == 404


def test_superseded_evidence_hides_the_claim(rig: Rig, world: World, gholder: Any) -> None:
    s = Setup(rig, world, gholder)
    with rig.admin.begin() as conn:
        conn.execute(
            text("UPDATE knowledge_revisions SET state = 'superseded' WHERE id = :r"),
            {"r": s.p1.revision_id},
        )
    got = world.api.get(f"{s.base()}/neighborhood?entity_id={s.a.id}", world.viewer)
    assert got.status == 200
    assert got.body["edges"] == []
    assert world.api.get(f"{s.base()}/claims/{s.ab.id}", world.viewer).status == 404
