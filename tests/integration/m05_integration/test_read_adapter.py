"""The graph read adapter on the in-memory store and on real RushDB: same visibility answers."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.infrastructure.rushdb.graph import RushDbGraphRepository
from app.infrastructure.rushdb.memory import InMemoryGraphRepository
from app.modules.graph.read_adapter import StoreGraphReadRepository
from app.modules.knowledge.ports import ClaimRecord, EntityRecord, EvidenceRecord
from tests.retrieval.support import make_scope

ORG, WS1, WS2, WS3 = (uuid.uuid4() for _ in range(4))


@pytest.fixture(params=["memory", "real"])
def store(request: pytest.FixtureRequest) -> Any:
    if request.param == "real":
        return RushDbGraphRepository(request.getfixturevalue("rushdb_settings"))
    return InMemoryGraphRepository()


def _ent(name: str, ws: uuid.UUID = WS1) -> EntityRecord:
    return EntityRecord(uuid.uuid4(), ORG, ws, "concept", name, [], "active", "v1", uuid.uuid4())


def _claim(
    s: EntityRecord, o: EntityRecord, *, ws: uuid.UUID = WS1, verification: str = "verified"
) -> ClaimRecord:
    return ClaimRecord(
        uuid.uuid4(),
        ORG,
        ws,
        uuid.uuid4(),
        uuid.uuid4(),
        s.id,
        "depends_on",
        o.id,
        None,
        0.9,
        verification,
        "v1",
        True,
    )


def _ev(c: ClaimRecord) -> EvidenceRecord:
    return EvidenceRecord(
        uuid.uuid4(),
        c.id,
        ORG,
        c.workspace_id,
        uuid.uuid4(),
        c.revision_id,
        c.document_id,
        uuid.uuid4(),
        "x",
    )


def test_reads_respect_workspace_published_and_verified(store: Any) -> None:
    scope = make_scope(ORG, [WS1, WS2])
    org = scope.org
    a, b, c, d = _ent("Atlas"), _ent("Garnet"), _ent("Zeta"), _ent("Foreign", WS3)
    other = _ent("atlas twin", WS3)
    good = _claim(a, b)
    unverified = _claim(a, c, verification="unverified")
    foreign = _claim(a, d, ws=WS3)
    unpublished = _claim(b, c)
    store.upsert_entities(org, [a, b, c, d, other])
    store.upsert_claims(org, [good, unverified, foreign], [_ev(good), _ev(foreign)], published=True)
    store.upsert_claims(org, [unpublished], [], published=False)

    repo = StoreGraphReadRepository(store)
    assert {x.id for x in repo.neighbors(scope, [a.id, b.id], 50)} == {good.id}
    assert repo.get_claim(scope, good.id) is not None
    for hidden in (unverified, foreign, unpublished):
        assert repo.get_claim(scope, hidden.id) is None
    evidence = repo.get_evidence(scope, [good.id, foreign.id])
    assert [e.claim_id for e in evidence] == [good.id]
    assert {e.id for e in repo.get_entities(scope, [a.id, d.id])} == {a.id}

    found = repo.search_entities(scope, "ATL", None, 10)  # casefold; in-scope workspaces only
    assert [e.canonical_name for e in found] == ["Atlas"]
    page = repo.search_entities(scope, "", None, 2)
    assert [e.canonical_name for e in page] == ["Atlas", "Garnet"]
    key = (page[-1].canonical_name.casefold(), page[-1].id)
    assert [e.canonical_name for e in repo.search_entities(scope, "", None, 5, key)] == ["Zeta"]
    stranger = make_scope(uuid.uuid4(), [WS1])  # another tenant sees nothing
    assert repo.neighbors(stranger, [a.id], 50) == []
    assert repo.search_entities(stranger, "", None, 10) == []
