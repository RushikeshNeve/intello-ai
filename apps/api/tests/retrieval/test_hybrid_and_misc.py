"""Hybrid retrieval degrade rules, snippets, rate limiter and log hygiene (no database)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from app.modules.ai.embeddings import EmbeddingUnavailable, FakeEmbeddingClient
from app.modules.knowledge.ports import (
    KnowledgeStoreError,
    KnowledgeStoreUnavailable,
    SearchFilters,
)
from app.modules.retrieval import SearchUnavailable, TokenBucketLimiter, gather, retrieve_candidates
from app.modules.retrieval.snippets import build_snippet, highlight_ranges, query_terms
from tests.retrieval.support import SPEC, index_document, make_scope, new_repo, org_scope

ORG, WS_A, WS_B = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _setup() -> tuple[Any, Any, FakeEmbeddingClient]:
    emb = FakeEmbeddingClient(SPEC)
    org = org_scope(ORG)
    repo = new_repo(org, emb)
    return org, repo, emb


def test_hybrid_returns_both_legs_and_scope_filters_other_workspaces() -> None:
    org, repo, emb = _setup()
    mine = index_document(repo, org, emb, WS_A, ["quarterly budget planning meeting notes"])
    theirs = index_document(repo, org, emb, WS_B, ["quarterly budget planning meeting notes"])
    scope = make_scope(ORG, [WS_A])
    found = retrieve_candidates(scope, repo, emb, "quarterly budget planning")
    assert set(found.rankings) == {"keyword", "vector"}
    assert found.degraded == ()
    for hits in found.rankings.values():
        ids = {h.document_id for h in hits}
        assert ids == {mine.document_id}
        assert theirs.document_id not in ids  # filtered before ranking, not after


def test_unpublished_chunks_are_not_candidates() -> None:
    org, repo, emb = _setup()
    index_document(repo, org, emb, WS_A, ["secret draft budget"], publish=False)
    scope = make_scope(ORG, [WS_A])
    found = retrieve_candidates(scope, repo, emb, "secret draft budget")
    assert all(not hits for hits in found.rankings.values())


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_embedder_down_degrades_to_keyword(mode: str) -> None:
    org, repo, emb = _setup()
    doc = index_document(repo, org, emb, WS_A, ["alpha beta gamma delta"])
    emb.fail_next(EmbeddingUnavailable("down"))
    scope = make_scope(ORG, [WS_A])
    found = retrieve_candidates(scope, repo, emb, "alpha beta", mode=mode)  # type: ignore[arg-type]
    assert found.degraded == ("embedding_unavailable",)
    assert found.mode == "keyword"
    assert set(found.rankings) == {"keyword"}
    assert found.rankings["keyword"][0].document_id == doc.document_id


def test_no_embedder_degrades_and_keyword_mode_never_embeds() -> None:
    org, repo, emb = _setup()
    index_document(repo, org, emb, WS_A, ["alpha beta gamma"])
    scope = make_scope(ORG, [WS_A])
    assert retrieve_candidates(scope, repo, None, "alpha").degraded == ("embedding_unavailable",)
    before = emb.query_calls
    found = retrieve_candidates(scope, repo, emb, "alpha", mode="keyword")
    assert found.degraded == ()
    assert emb.query_calls == before


def test_vector_only_mode() -> None:
    org, repo, emb = _setup()
    index_document(repo, org, emb, WS_A, ["alpha beta gamma"])
    scope = make_scope(ORG, [WS_A])
    found = retrieve_candidates(scope, repo, emb, "alpha beta", mode="vector")
    assert set(found.rankings) == {"vector"}


class _DownRepo:
    def search_keyword(self, *a: Any, **k: Any) -> Any:
        raise KnowledgeStoreUnavailable("down")

    search_vector = search_keyword


class _BrokenRepo:
    def search_keyword(self, *a: Any, **k: Any) -> Any:
        raise KnowledgeStoreError("boom", code="invalid_request")


class _NoIndexRepo:
    def search_keyword(self, *a: Any, **k: Any) -> Any:
        raise KnowledgeStoreError("not found", code="not_found")

    def search_vector(self, *a: Any, **k: Any) -> Any:
        raise KnowledgeStoreError("missing", code="index_missing")


def test_index_down_is_search_unavailable_not_empty() -> None:
    scope = make_scope(ORG, [WS_A])
    emb = FakeEmbeddingClient(SPEC)
    with pytest.raises(SearchUnavailable):
        retrieve_candidates(scope, _DownRepo(), emb, "q")  # type: ignore[arg-type]
    with pytest.raises(SearchUnavailable):
        retrieve_candidates(scope, _BrokenRepo(), emb, "q", mode="keyword")  # type: ignore[arg-type]


def test_missing_vector_index_means_nothing_indexed_not_degraded() -> None:
    scope = make_scope(ORG, [WS_A])
    found = retrieve_candidates(scope, _NoIndexRepo(), FakeEmbeddingClient(SPEC), "q")  # type: ignore[arg-type]
    assert found.degraded == ()
    assert found.rankings == {"keyword": [], "vector": []}


def test_every_search_carries_the_scope_filters() -> None:
    seen: list[SearchFilters] = []

    class Spy:
        def search_keyword(self, scope: Any, q: str, filters: SearchFilters, *, limit: int) -> Any:
            seen.append(filters)
            return []

        def search_vector(
            self, scope: Any, s: str, v: Any, filters: SearchFilters, *, limit: int
        ) -> Any:
            seen.append(filters)
            return []

    scope = make_scope(ORG, [WS_A, WS_B])
    retrieve_candidates(scope, Spy(), FakeEmbeddingClient(SPEC), "query")  # type: ignore[arg-type]
    assert len(seen) == 2
    assert all(f.workspace_ids == scope.workspace_ids for f in seen)


def test_query_text_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    org, repo, emb = _setup()
    index_document(repo, org, emb, WS_A, ["zebra unicorn gryphon"])
    scope = make_scope(ORG, [WS_A])
    with caplog.at_level(logging.DEBUG):
        gather(scope, repo, emb, "zebra unicorn secretword")
    assert "secretword" not in caplog.text


# --------------------------------------------------------------------------- snippets


def test_highlight_ranges_whole_words_case_insensitive_and_merged() -> None:
    text = "Budget planning: the budget plan is not planning-free."
    terms = query_terms("budget planning")
    assert highlight_ranges(text, terms) == [[0, 6], [7, 15], [21, 27], [40, 48]]
    assert highlight_ranges("budgetary", ["budget"]) == []
    assert highlight_ranges("anything", []) == []


def test_query_terms_drop_stop_words_and_dupes() -> None:
    assert query_terms("The Budget of the budget, 7") == ["budget", "7"]


def test_snippet_window_keeps_the_match_and_highlights_inside_it() -> None:
    body = ("filler " * 80) + "the needle is here " + ("tail " * 80)
    snippet = build_snippet(body, ["needle"])
    assert len(snippet) <= 330
    assert snippet.startswith("…")
    assert snippet.endswith("…")
    (rng,) = highlight_ranges(snippet, ["needle"])
    assert snippet[rng[0] : rng[1]] == "needle"
    assert build_snippet("short text", ["short"]) == "short text"


# ----------------------------------------------------------------------------- limiter


def test_token_bucket_burst_refill_and_retry_after() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(3, clock=lambda: now[0])  # 3/min = 1 token / 20 s
    assert [limiter.acquire("u") for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = limiter.acquire("u")
    assert wait == pytest.approx(20.0)
    assert limiter.acquire("other") == 0.0  # keys are independent
    now[0] = 19.0
    assert limiter.acquire("u") > 0
    now[0] = 41.0
    assert limiter.acquire("u") == 0.0
    now[0] = 10_000.0  # idle buckets refill to capacity, never beyond
    assert [limiter.acquire("u") for _ in range(4)][-1] > 0


def test_limiter_rejects_a_zero_rate() -> None:
    with pytest.raises(ValueError, match="rate_per_min"):
        TokenBucketLimiter(0)
