"""M05.02 sanity tests: traversal safety, router selectivity, evidence merge."""

from __future__ import annotations

import uuid

import pytest

from app.modules.graph.fake_read import FakeChunkSource, FakeGraphRead
from app.modules.graph.merge import GraphEvidence, augment_evidence
from app.modules.graph.ports_read import GClaim, GEntity, GEvidence
from app.modules.graph.read_settings import GraphReadSettings
from app.modules.graph.router import classify, route
from app.modules.graph.traversal import traverse
from tests.graph_retrieval.support import (
    ORG,
    WS1,
    WS2,
    claim,
    db_for,
    ent,
    evidence_for,
    pub_rows,
    scope_of,
)

# ------------------------------------------------------------------------------- traversal


@pytest.mark.parametrize("leaky", [False, True])
def test_traversal_never_returns_hidden_records(leaky: bool) -> None:
    a, b, c, d, e = ent("A"), ent("B"), ent("C"), ent("D"), ent("E", WS2)
    x_org = ent("X", org=uuid.uuid4())
    claims = [
        claim(a, b),  # ok
        claim(b, c, verified=False),  # unverified: never
        claim(a, d, published=False),  # unpublished: never
        claim(a, e, ws=WS1),  # cross-workspace endpoint (WS2 not in scope): never
        claim(a, x_org),  # other org: never
    ]
    repo = FakeGraphRead([a, b, c, d, e, x_org], claims, leaky=leaky)
    sub = traverse(scope_of(WS1), repo, [a.id])
    assert {n.canonical_name for n in sub.nodes.values()} == {"A", "B"}
    assert [(x.subject_id, x.object_id) for x in sub.edges] == [(a.id, b.id)]
    assert sub.degree(a.id) == 1  # degree counts authorized edges only


def test_cross_workspace_edge_is_not_followed_even_when_both_in_scope() -> None:
    a, e = ent("A", WS1), ent("E", WS2)
    repo = FakeGraphRead([a, e], [claim(a, e, ws=WS1)])
    sub = traverse(scope_of(WS1, WS2), repo, [a.id])
    assert sub.edges == []
    assert set(sub.nodes) == {a.id}


def test_depth_node_caps_and_cycles() -> None:
    chain = [ent(f"N{i}") for i in range(6)]
    repo = FakeGraphRead(chain, [claim(chain[i], chain[i + 1]) for i in range(5)])
    sub = traverse(scope_of(), repo, [chain[0].id], depth=9)  # depth is clamped to 2
    assert {n.canonical_name for n in sub.nodes.values()} == {"N0", "N1", "N2"}

    hub = ent("hub")
    leaves = [ent(f"L{i}") for i in range(10)]
    repo = FakeGraphRead([hub, *leaves], [claim(hub, leaf) for leaf in leaves])
    sub = traverse(scope_of(), repo, [hub.id], max_nodes=4)
    assert len(sub.nodes) == 4
    assert sub.truncated
    sub = traverse(scope_of(), repo, [hub.id], max_edges=3)
    assert len(sub.edges) == 3
    assert sub.truncated

    x, y, z = ent("x"), ent("y"), ent("z")
    ring = FakeGraphRead([x, y, z], [claim(x, y), claim(y, z), claim(z, x)])
    assert len(traverse(scope_of(), ring, [x.id]).edges) == 3  # terminates


# ---------------------------------------------------------------------------------- router


def test_basic_question_never_touches_the_graph() -> None:
    a = ent("Atlas")
    repo = FakeGraphRead([a], [])
    for q in ("What laptop do new engineers get?", "How much is the on-call allowance?"):
        assert route(scope_of(), repo, q).route == "basic"
    assert route(scope_of(), repo, "Which services depend on Atlas?", mode="off").route == "basic"
    assert repo.calls == []  # spy: no graph call at all


def test_relationship_question_and_force_use_the_graph() -> None:
    a, b = ent("Atlas"), ent("Garnet")
    repo = FakeGraphRead([a, b], [])
    d = route(scope_of(), repo, "Which service is related to Atlas and Garnet?")
    assert d.route == "graph"
    assert set(d.seeds) == {a.id, b.id}
    assert "search_entities" in repo.calls
    assert classify("What is the pension match?", "force") == "graph"
    assert route(scope_of(), repo, "Tell me about Atlas", mode="force").seeds == (a.id,)
    # a cue but no known entity: falls back to basic
    assert route(scope_of(), repo, "what is related to Zebra?").route == "basic"


# ----------------------------------------------------------------------------------- merge


def _world() -> tuple[GEntity, GEntity, GClaim, GEvidence, uuid.UUID, FakeGraphRead]:
    a, b = ent("Atlas"), ent("Garnet")
    c1 = claim(a, b, predicate="related_to")
    ev, chunk = evidence_for(c1)
    repo = FakeGraphRead([a, b], [c1], [ev])
    return a, b, c1, ev, chunk, repo


def test_merge_cites_original_chunks_only_and_tags_via_graph() -> None:
    _a, _b, c1, ev, chunk, repo = _world()
    session = db_for(pub_rows([(ev, c1)]))
    chunks = FakeChunkSource({chunk: "Atlas and Garnet are both owned by the Core team."})
    out = augment_evidence(
        session,
        scope_of(),
        "Which service is related to Atlas?",
        [],
        repo,
        chunks,
        settings=GraphReadSettings(),
    )
    assert len(out) == 1
    assert isinstance(out[0], GraphEvidence)
    g = out[0]
    assert g.via_graph
    assert g.chunk_id == chunk
    assert "Atlas" in g.path
    assert "Garnet" in g.path
    assert g.text.startswith("Atlas and Garnet")  # chunk text, not graph text
    assert g.evidence_id == "e1"
    assert g.workspace_id == WS1


def test_merge_drops_unpublished_evidence_and_basic_is_unchanged() -> None:
    _a, _b, c1, ev, chunk, repo = _world()
    chunks = FakeChunkSource({chunk: "text"})
    gone = db_for([])  # revision no longer published in PostgreSQL
    q = "Which service is related to Atlas?"
    assert (
        augment_evidence(gone, scope_of(), q, [], repo, chunks, settings=GraphReadSettings()) == []
    )
    deleted = FakeChunkSource({})  # artifact deleted
    live = db_for(pub_rows([(ev, c1)]))
    assert (
        augment_evidence(live, scope_of(), q, [], repo, deleted, settings=GraphReadSettings()) == []
    )
    repo.calls.clear()
    assert augment_evidence(live, scope_of(), "What is the pension match?", [], repo, chunks) == []
    assert repo.calls == []


def test_merge_failure_degrades_to_base() -> None:
    class Boom(FakeGraphRead):
        def search_entities(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("graph down")

    out = augment_evidence(
        db_for([]),
        scope_of(),
        "related to Atlas",
        [],
        Boom(),
        FakeChunkSource({}),
        settings=GraphReadSettings(),
    )
    assert out == []
    assert ORG  # keep import used
