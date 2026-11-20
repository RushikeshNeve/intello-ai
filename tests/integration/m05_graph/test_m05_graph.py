"""M05.01: graph build end to end on real PostgreSQL (+ RLS) and both stores (fake and real RushDB).

Covers: extraction task (verified/unverified claims, idempotent re-run, fencing), supersede removes
graph records, entity resolution (exact merge, fuzzy -> candidate only, same name other type stays
separate), the merge-candidate routes (403/404/409, cross-workspace, cross-org) and the grants of
the two new tables.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from catalog_support import World
from ret_support import Rig
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.modules.ai.models.fake import FakeChatModel
from app.modules.graph.extract import ExtractedEntity
from app.modules.graph.ontology import normalize_name
from app.modules.graph.resolve import resolve_entities
from app.modules.graph.tasks import extract_revision
from app.modules.knowledge.publication import reconcile_document
from app.workers.worker_db import session_factory_for

TEXTS = [
    "Alice Smith works for Acme Corporation in Berlin.",
    "Bob Jones leads the Mercury project at Initech.",
]


def model_reply() -> str:
    return json.dumps(
        {
            "entities": [
                {"name": "Alice Smith", "type": "person", "chunk": 1},
                {"name": "Acme Corporation", "type": "organization", "chunk": 1},
                {"name": "Bob Jones", "type": "person", "chunk": 2},
                {"name": "Initech", "type": "organization", "chunk": 2},
            ],
            "relations": [
                {
                    "subject": "Alice Smith",
                    "predicate": "works_for",
                    "object": "Acme Corporation",
                    "chunk": 1,
                },
                # claimed from the wrong chunk: nothing in chunk 1 supports it
                {"subject": "Bob Jones", "predicate": "works_for", "object": "Initech", "chunk": 1},
            ],
        }
    )


def run_task(
    rig: Rig,
    worker_engine: Engine,
    settings: Settings,
    world: World,
    doc: Any,
    revision: uuid.UUID,
    *,
    enabled: bool = True,
) -> Any:
    sessions = session_factory_for(worker_engine)
    return extract_revision(
        sessions,
        rig.repo,
        lambda _org: FakeChatModel([model_reply()]),
        settings if enabled else settings.model_copy(update={"GRAPH_ENABLED": False}),
        world.org_id,
        doc.id,
        revision,
    )


def entity_rows(rig: Rig, ws: uuid.UUID) -> list[Any]:
    return rig.q(
        "SELECT type, normalized_name, state FROM graph_entities WHERE workspace_id = :w", w=ws
    )


# ------------------------------------------------------------------------------ the task


def test_task_builds_verified_graph_and_is_idempotent(
    rig: Rig, world: World, worker_engine: Engine, graph_settings: Settings
) -> None:
    doc = rig.seed_world(world, title="People")
    pub = rig.publish(world.org_id, world.ws, doc, TEXTS)
    scope = rig.org_scope(world.org_id)

    assert (
        run_task(
            rig, worker_engine, graph_settings, world, doc, pub.revision_id, enabled=False
        ).status
        == "disabled"
    )
    assert rig.repo.list_claims(scope, pub.revision_id) == []

    out = run_task(rig, worker_engine, graph_settings, world, doc, pub.revision_id)
    assert (out.status, out.entities, out.claims, out.verified) == ("built", 4, 2, 1)
    claims = rig.repo.list_claims(scope, pub.revision_id)
    by_state = {c.verification: c for c in claims}
    assert set(by_state) == {"verified", "unverified"}
    assert all(c.published for c in claims)
    good = by_state["verified"]
    assert good.predicate == "works_for"
    assert good.document_id == doc.id
    (evidence,) = rig.repo.list_evidence(scope, good.id)
    assert evidence.chunk_id == pub.chunk_ids[0]
    assert evidence.version_id == pub.version_id
    assert len(rig.repo.list_entities(scope, world.ws)) == 4
    assert len(entity_rows(rig, world.ws)) == 4

    again = run_task(rig, worker_engine, graph_settings, world, doc, pub.revision_id)
    assert (again.status, again.claims) == ("built", 2)
    assert sorted(c.id for c in rig.repo.list_claims(scope, pub.revision_id)) == sorted(
        c.id for c in claims
    )
    assert len(entity_rows(rig, world.ws)) == 4


def test_task_is_fenced_by_the_published_pointer(
    rig: Rig, world: World, worker_engine: Engine, graph_settings: Settings
) -> None:
    doc = rig.seed_world(world, title="Two versions", versions=2)
    v1 = rig.publish(world.org_id, world.ws, doc, TEXTS, version=1)
    v2 = rig.publish(world.org_id, world.ws, doc, TEXTS, version=2)  # v1 is superseded
    scope = rig.org_scope(world.org_id)
    stale = run_task(rig, worker_engine, graph_settings, world, doc, v1.revision_id)
    assert stale.status == "stale"
    assert rig.repo.list_claims(scope, v1.revision_id) == []
    assert entity_rows(rig, world.ws) == []  # nothing resolved either

    # The pointer moves while the graph is being written: the write is taken back.
    def supersede() -> None:
        with rig.admin.begin() as conn:
            conn.execute(
                text("UPDATE knowledge_revisions SET state = 'superseded' WHERE id = :r"),
                {"r": v2.revision_id},
            )

    rig.repo.after["upsert_claims"] = supersede
    raced = run_task(rig, worker_engine, graph_settings, world, doc, v2.revision_id)
    assert raced.status == "stale"
    assert rig.repo.list_claims(scope, v2.revision_id) == []


def test_supersede_reconcile_removes_graph_records(
    rig: Rig, world: World, worker_engine: Engine, graph_settings: Settings
) -> None:
    doc = rig.seed_world(world, title="Superseded", versions=2)
    v1 = rig.publish(world.org_id, world.ws, doc, TEXTS, version=1)
    assert (
        run_task(rig, worker_engine, graph_settings, world, doc, v1.revision_id).status == "built"
    )
    scope = rig.org_scope(world.org_id)
    assert len(rig.repo.list_claims(scope, v1.revision_id)) == 2
    rig.publish(world.org_id, world.ws, doc, TEXTS, version=2)
    reconcile_document(
        session_factory_for(worker_engine), rig.repo, world.org_id, doc.id, artifacts=rig.artifacts
    )
    assert rig.repo.list_claims(scope, v1.revision_id) == []
    assert rig.repo.count_chunks(scope, v1.revision_id) == 0


# ----------------------------------------------------------------------------- resolution


def ent(name: str, kind: str) -> ExtractedEntity:
    return ExtractedEntity(type=kind, name=name, normalized=normalize_name(name), surfaces={name})


def resolve(worker_engine: Engine, world: World, *entities: ExtractedEntity) -> Any:
    sessions = session_factory_for(worker_engine)
    with sessions(world.org_id) as session:
        return resolve_entities(session, world.org_id, world.ws, entities)


def test_resolution_merges_exact_and_only_proposes_fuzzy(
    mrig: Rig, world: World, worker_engine: Engine
) -> None:
    first = resolve(worker_engine, world, ent("Acme Corporation", "organization"))
    again = resolve(
        worker_engine, world, ent("ACME  corporation!", "organization")
    )  # exact after normalization
    assert list(first.ids.values()) == list(again.ids.values())
    assert again.entities[0].aliases == ["ACME  corporation!"]
    typo = resolve(worker_engine, world, ent("Acme Corporatoin", "organization"))
    assert typo.candidates == 1
    assert list(typo.ids.values()) != list(first.ids.values())  # not merged
    pending = mrig.q(
        "SELECT state, score FROM entity_merge_candidates WHERE workspace_id = :w", w=world.ws
    )
    assert [(p.state, round(p.score, 1)) for p in pending] == [("pending", 0.9)]
    # same name, other type: separate identity, never a candidate, listed as a conflict
    other = resolve(worker_engine, world, ent("Acme Corporation", "project"))
    assert other.candidates == 0
    assert list(other.ids.values()) != list(first.ids.values())
    states = {(r.type, r.state) for r in entity_rows(mrig, world.ws)}
    assert states == {("organization", "active"), ("project", "active")}


# ---------------------------------------------------------------------------------- routes


def candidate_path(world: World, sub: str = "", ws: uuid.UUID | None = None) -> str:
    return f"/v1/orgs/{world.org_id}/workspaces/{ws or world.ws}/graph/merge-candidates{sub}"


def test_merge_candidate_routes(mrig: Rig, world: World, worker_engine: Engine) -> None:
    resolve(
        worker_engine,
        world,
        ent("Initech Systems", "organization"),
        ent("Initech Systemz", "organization"),
        ent("Mercury", "project"),
        ent("Mercury", "concept"),
    )
    api = world.api
    listing = api.get(candidate_path(world), world.editor)
    assert listing.status == 200, listing.body
    (item,) = listing.body["items"]
    assert {item["entity_a"]["type"], item["entity_b"]["type"]} == {"organization"}
    assert [c["normalized_name"] for c in listing.body["conflicts"]] == ["mercury"]
    cid = item["id"]
    url = candidate_path(world, f"/{cid}/decision")

    # roles and concealment
    assert api.post(url, world.viewer, {"decision": "accept"}).status == 403
    assert api.get(candidate_path(world), world.viewer).status == 403
    assert api.post(url, world.t.outsider, {"decision": "accept"}).status == 404
    assert api.post(url, world.foreign.owner, {"decision": "accept"}).status == 404  # cross-org
    ws2 = api.create_workspace(world.org_id, world.t.owner)
    wrong_ws = api.post(
        candidate_path(world, f"/{cid}/decision", ws2), world.t.owner, {"decision": "accept"}
    )
    assert wrong_ws.status == 404  # cross-workspace: the candidate is not in that workspace
    assert api.post(url, world.editor, {"decision": "maybe"}).status == 422
    assert (
        mrig.q("SELECT state FROM entity_merge_candidates WHERE id = :i", i=cid)[0].state
        == "pending"
    )

    res = api.post(url, world.editor, {"decision": "accept"})
    assert res.status == 200, res.body
    assert res.body["state"] == "accepted"
    winner = res.body["merged_into"]
    rows = mrig.q(
        "SELECT id, state, merged_into, aliases FROM graph_entities"
        " WHERE type = 'organization' AND workspace_id = :w",
        w=world.ws,
    )
    merged = [r for r in rows if r.state == "merged"]
    assert len(merged) == 1
    assert str(merged[0].merged_into) == winner
    assert [r["event_type"] for r in api.audit(res.request_id)] == ["graph.merge.decided"]
    assert api.post(url, world.editor, {"decision": "reject"}).status == 409  # already decided


# -------------------------------------------------------------------------------- grants


def test_new_tables_grants_are_least_privilege(
    mrig: Rig, worker_engine: Engine, app_engine: Engine, world: World
) -> None:
    for engine in (worker_engine, app_engine):
        with engine.connect() as conn, pytest.raises(DBAPIError) as err:
            conn.execute(text("DELETE FROM graph_entities"))
        assert "permission denied" in str(err.value)
    with app_engine.connect() as conn, pytest.raises(DBAPIError) as err:
        conn.execute(
            text(
                "INSERT INTO graph_entities (id, org_id, workspace_id, type, canonical_name,"
                " normalized_name) VALUES (gen_random_uuid(), :o, :w, 'person', 'x1', 'x1')"
            ),
            {"o": world.org_id, "w": world.ws},
        )
    assert "permission denied" in str(err.value)
