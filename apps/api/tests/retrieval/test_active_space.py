"""Search follows the org's ACTIVE embedding space (M12.05 follow-up): cutover/rollback switch the
space queried; an embedder that does not match it fails safely (no database)."""

from __future__ import annotations

import uuid

import pytest

from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.modules.ai.embeddings import EmbeddingModelSpec, FakeEmbeddingClient
from app.modules.retrieval import SearchUnavailable, retrieve_candidates
from app.modules.retrieval.hybrid import EmbeddingSpaceMismatch
from tests.retrieval.support import SPEC, index_document, make_scope, new_repo, org_scope

ORG, WS = uuid.uuid4(), uuid.uuid4()
SPEC_B = EmbeddingModelSpec(
    model_id="fake-embed-b",
    dimensions=32,
    query_prefix="q: ",
    document_prefix="d: ",
    max_input_chars=6000,
    spec_version=1,
)


def _two_spaces() -> tuple[
    InMemoryKnowledgeRepository, FakeEmbeddingClient, FakeEmbeddingClient, uuid.UUID, uuid.UUID
]:
    a, b = FakeEmbeddingClient(SPEC), FakeEmbeddingClient(SPEC_B)
    org = org_scope(ORG)
    repo = new_repo(org, a)
    repo.ensure_indexes(org, b.spec.space_id, b.spec.dimensions)
    doc_a = index_document(repo, org, a, WS, ["alpha shared text"])
    doc_b = index_document(repo, org, b, WS, ["alpha shared text"])
    return repo, a, b, doc_a.document_id, doc_b.document_id


def _vector_doc(
    repo: InMemoryKnowledgeRepository, emb: FakeEmbeddingClient, active: str | None
) -> uuid.UUID:
    scope = make_scope(ORG, [WS], active_space=active)
    found = retrieve_candidates(scope, repo, emb, "alpha shared", mode="vector")
    return found.rankings["vector"][0].document_id


def test_cutover_and_rollback_switch_the_queried_space() -> None:
    repo, a, b, doc_a, doc_b = _two_spaces()
    # no pointer row: configured default; with embedder A that is space A
    assert _vector_doc(repo, a, None) == doc_a
    # cutover: pointer -> B, deployed embedder is B
    assert _vector_doc(repo, b, SPEC_B.space_id) == doc_b
    # rollback: pointer -> A again, embedder A
    assert _vector_doc(repo, a, SPEC.space_id) == doc_a


def test_dimension_mismatch_fails_safely_without_touching_the_index() -> None:
    repo, a, _b, _da, _db = _two_spaces()
    scope = make_scope(ORG, [WS], active_space=SPEC_B.space_id)  # org is on B, embedder is A
    for mode in ("hybrid", "vector"):
        with pytest.raises(EmbeddingSpaceMismatch):
            retrieve_candidates(scope, repo, a, "alpha", mode=mode)
    assert issubclass(EmbeddingSpaceMismatch, SearchUnavailable)  # routers answer 503
    assert a.query_calls == 0  # never embedded for the wrong space
    # keyword search does not need an embedding and keeps working
    assert retrieve_candidates(scope, repo, a, "alpha", mode="keyword").rankings["keyword"]
