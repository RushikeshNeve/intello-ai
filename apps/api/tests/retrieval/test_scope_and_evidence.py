"""Scope guard, revalidation and the Evidence guard (no database)."""

from __future__ import annotations

import uuid

import pytest

from app.modules.ai.embeddings import FakeEmbeddingClient
from app.modules.knowledge.ports import UnknownOrganization
from app.modules.retrieval import (
    EmptyScope,
    Evidence,
    RetrievalScope,
    gather,
    resolve_scope,
    revalidate,
)
from tests.retrieval.support import (
    SPEC,
    FakeDb,
    index_document,
    make_scope,
    new_repo,
    org_scope,
    pub_row,
)


def test_scope_cannot_be_constructed_directly() -> None:
    org = uuid.uuid4()
    with pytest.raises(TypeError):
        RetrievalScope(org, uuid.uuid4(), frozenset({uuid.uuid4()}), org_scope(org))
    with pytest.raises(TypeError):
        RetrievalScope(  # a forged mint token is not the real one
            org, uuid.uuid4(), frozenset({uuid.uuid4()}), org_scope(org), _mint=object()
        )


def test_scope_is_derived_and_filters_match() -> None:
    org, w1, w2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scope = make_scope(org, [w1, w2])
    assert scope.workspace_ids == frozenset({w1, w2})
    assert scope.filters().workspace_ids == frozenset({w1, w2})


def test_requested_ids_only_narrow_and_forged_ids_are_ignored() -> None:
    org, w1, w2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    forged = uuid.uuid4()
    scope = make_scope(org, [w1, w2], requested=[w1, forged])
    assert scope.workspace_ids == frozenset({w1})


def test_only_forged_ids_give_empty_scope() -> None:
    org = uuid.uuid4()
    with pytest.raises(EmptyScope):
        make_scope(org, [uuid.uuid4()], requested=[uuid.uuid4()])
    with pytest.raises(EmptyScope):
        make_scope(org, [])


def test_non_member_is_unknown_organization() -> None:
    org = uuid.uuid4()
    db = FakeDb(org_id=org, role=None, workspaces=[uuid.uuid4()])
    with pytest.raises(UnknownOrganization):
        resolve_scope(db.session, org_id=org, user_id=uuid.uuid4())
    with pytest.raises(UnknownOrganization):  # an org the session cannot see
        resolve_scope(db.session, org_id=uuid.uuid4(), user_id=uuid.uuid4())


def _world() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, FakeEmbeddingClient]:
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), FakeEmbeddingClient(SPEC)


def test_revalidate_is_one_batched_query_and_numbers_evidence() -> None:
    org_id, w, _other, emb = _world()
    org = org_scope(org_id)
    repo = new_repo(org, emb)
    docs = [index_document(repo, org, emb, w, [f"alpha topic {i} words"]) for i in range(6)]
    scope = make_scope(org_id, [w])
    gathered = gather(scope, repo, emb, "alpha topic", candidates=20)
    db = FakeDb(org_id=org_id, published=[pub_row(d, w, title=f"T{i}") for i, d in enumerate(docs)])
    evidence, dropped = revalidate(db.session, scope, gathered.candidates, n_lists=gathered.n_lists)
    assert len(evidence) == 6
    assert dropped == 0
    assert len(db.statements) == 1  # no N+1
    assert [e.evidence_id for e in evidence] == [f"e{n}" for n in range(1, 7)]
    assert all(0.0 < e.score <= 1.0 for e in evidence)


def test_revalidate_drops_unpublished_deleted_and_mismatched() -> None:
    org_id, w, other_w, emb = _world()
    org = org_scope(org_id)
    repo = new_repo(org, emb)
    live = index_document(repo, org, emb, w, ["shared keyword live"])
    gone = index_document(repo, org, emb, w, ["shared keyword gone"])
    moved = index_document(repo, org, emb, w, ["shared keyword moved"])
    foreign = index_document(repo, org, emb, w, ["shared keyword foreign"])
    scope = make_scope(org_id, [w])
    gathered = gather(scope, repo, emb, "shared keyword", candidates=20)
    rows = [pub_row(live, w), pub_row(moved, w), pub_row(foreign, other_w)]
    rows[1].version_id = uuid.uuid4()  # index says version X, PostgreSQL says version Y
    db = FakeDb(org_id=org_id, published=rows)  # `gone` has no published row any more
    evidence, dropped = revalidate(db.session, scope, gathered.candidates)
    assert [e.document_id for e in evidence] == [live.document_id]
    assert dropped == 3
    assert gone.document_id not in {e.document_id for e in evidence}


def test_revalidate_with_nothing_runs_no_query() -> None:
    org_id, w, _o, _e = _world()
    scope = make_scope(org_id, [w])
    db = FakeDb(org_id=org_id)
    assert revalidate(db.session, scope, []) == ([], 0)
    assert db.statements == []


def test_evidence_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError):
        Evidence(
            evidence_id="e1",
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            revision_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            title="t",
            text="x",
            score=1.0,
            locators=(),
            heading_path=(),
            page_start=None,
            page_end=None,
            document_version_number=1,
            content_type="text/plain",
            workspace_slug="w",
        )


def test_a_stale_revision_chunk_cannot_shadow_the_live_one() -> None:
    org_id, w, _other, emb = _world()
    org = org_scope(org_id)
    repo = new_repo(org, emb)
    doc = uuid.uuid4()
    stale = index_document(repo, org, emb, w, ["heron migration schedule old"], document_id=doc)
    live = index_document(repo, org, emb, w, ["heron migration schedule new"], document_id=doc)
    scope = make_scope(org_id, [w])
    gathered = gather(scope, repo, emb, "heron migration schedule", candidates=20)
    db = FakeDb(org_id=org_id, published=[pub_row(live, w)])
    evidence, dropped = revalidate(
        db.session, scope, gathered.candidates, n_lists=gathered.n_lists, max_per_document=3
    )
    assert [e.revision_id for e in evidence] == [live.revision_id]
    assert dropped == 1
    assert stale.revision_id not in {e.revision_id for e in evidence}
