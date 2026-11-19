"""Contract suite of the ``KnowledgeRepository`` port, run against every implementation.

Subclass :class:`ContractTests` and provide two fixtures:

* ``repo``: the implementation under test;
* ``new_scope``: a factory returning a scope of a fresh, live organization every call.

Used by ``test_contract_memory.py`` (in-memory fake) and, against real RushDB 3.0.10 and PostgreSQL,
by ``tests/integration/m03_rushdb/test_contract_rushdb.py``.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from itertools import pairwise

import pytest

from app.modules.knowledge.ports import (
    KnowledgeRepository,
    KnowledgeStoreError,
    SearchFilters,
)
from tests.infrastructure_rushdb.support import DIMS, SPACE, ScopeFactory, make_chunk, make_revision

E1 = [1.0, 0.0, 0.0, 0.0]
E2 = [0.0, 1.0, 0.0, 0.0]
E3 = [0.0, 0.0, 1.0, 0.0]
E4 = [0.0, 0.0, 0.0, 1.0]


def _filters(*workspaces: uuid.UUID) -> SearchFilters:
    return SearchFilters(frozenset(workspaces))


def _ids(hits: list) -> list[uuid.UUID]:  # type: ignore[type-arg]
    return [h.chunk_id for h in hits]


class ContractTests:
    """Behaviour every ``KnowledgeRepository`` must have (not collected: no ``Test`` prefix)."""

    # ------------------------------------------------------------------ CRUD round trip

    def test_crud_round_trip(self, repo: KnowledgeRepository, new_scope: ScopeFactory) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        doc, rev, chunks, vectors = make_revision(
            scope,
            ws,
            ["alpha lighthouse keeper", "beta harbor tide", "gamma quiet orchard"],
            [E1, E2, E3],
        )
        assert repo.count_chunks(scope, rev) == 0
        assert repo.upsert_chunks(scope, chunks, vectors) == 3
        assert repo.count_chunks(scope, rev) == 3
        assert repo.count_chunks(scope, rev, published=False) == 3
        assert repo.count_chunks(scope, rev, published=True) == 0
        assert repo.list_chunk_ids(scope, rev) == [c.chunk_id for c in chunks]

        assert repo.set_published(scope, rev, True) == 3
        assert repo.count_chunks(scope, rev, published=True) == 3

        hits = repo.search_keyword(scope, "lighthouse", _filters(ws))
        assert _ids(hits) == [chunks[0].chunk_id]
        hit = hits[0]
        assert hit.document_id == doc
        assert hit.version_id == chunks[0].version_id
        assert hit.revision_id == rev
        assert hit.workspace_id == ws
        assert hit.text == "alpha lighthouse keeper"
        assert hit.locators == [{"type": "page", "page": 1}]
        assert hit.heading_path == ["Intro"]
        assert (hit.page_start, hit.page_end) == (1, 1)
        assert hit.score > 0
        assert math.isfinite(hit.score)

        assert repo.delete_revision(scope, rev).chunks == 3
        assert repo.count_chunks(scope, rev) == 0
        assert repo.search_keyword(scope, "lighthouse", _filters(ws)) == []

    def test_optional_fields_round_trip(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _doc, rev, chunks, vectors = make_revision(scope, ws, ["plain text without pages"], [E1])
        bare = replace(chunks[0], page_start=None, page_end=None, locators=[], heading_path=[])
        repo.upsert_chunks(scope, [bare], vectors)
        repo.set_published(scope, rev, True)
        (hit,) = repo.search_keyword(scope, "plain", _filters(ws))
        assert hit.page_start is None
        assert hit.page_end is None
        assert hit.locators == []
        assert hit.heading_path == []

    # ------------------------------------------------------------------- idempotency

    def test_reupsert_is_idempotent_and_never_unpublishes(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, vectors = make_revision(scope, ws, ["one two", "three four"], [E1, E2])
        repo.upsert_chunks(scope, chunks, vectors)
        repo.upsert_chunks(scope, chunks, vectors)
        assert repo.count_chunks(scope, rev) == 2
        assert repo.list_chunk_ids(scope, rev) == [c.chunk_id for c in chunks]
        assert repo.count_chunks(scope, rev, published=False) == 2

        repo.set_published(scope, rev, True)
        repo.upsert_chunks(scope, chunks, vectors)  # a stale duplicate task after publication
        assert repo.count_chunks(scope, rev) == 2
        assert repo.count_chunks(scope, rev, published=True) == 2
        assert _ids(repo.search_keyword(scope, "three", _filters(ws))) == [chunks[1].chunk_id]

    def test_reupsert_overwrites_content_and_vector(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, _v = make_revision(scope, ws, ["original words"], [E1])
        repo.upsert_chunks(scope, chunks, [E1])
        repo.set_published(scope, rev, True)
        changed = replace(chunks[0], text="replacement words")
        repo.upsert_chunks(scope, [changed], [E2])
        assert repo.count_chunks(scope, rev) == 1
        assert repo.search_keyword(scope, "original", _filters(ws)) == []
        assert _ids(repo.search_keyword(scope, "replacement", _filters(ws))) == [changed.chunk_id]
        assert _ids(repo.search_vector(scope, SPACE, E2, _filters(ws), limit=1)) == [
            changed.chunk_id
        ]

    def test_set_published_is_idempotent_and_reversible(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, vectors = make_revision(scope, ws, ["red apple", "green apple"], [E1, E2])
        repo.upsert_chunks(scope, chunks, vectors)
        f = _filters(ws)
        assert repo.search_keyword(scope, "apple", f) == []
        assert repo.set_published(scope, rev, True) == 2
        assert repo.set_published(scope, rev, True) == 0
        assert len(repo.search_keyword(scope, "apple", f)) == 2
        assert repo.set_published(scope, rev, False) == 2
        assert repo.set_published(scope, rev, False) == 0
        assert repo.search_keyword(scope, "apple", f) == []
        assert repo.search_vector(scope, SPACE, E1, f) == []
        assert repo.search_hybrid(scope, SPACE, "apple", E1, f) == []
        assert repo.count_chunks(scope, rev, published=False) == 2

    def test_set_published_touches_only_its_revision(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        doc, rev1, c1, v1 = make_revision(scope, ws, ["old plum"], [E1])
        _d, rev2, c2, v2 = make_revision(scope, ws, ["new plum"], [E2], document_id=doc)
        repo.upsert_chunks(scope, c1 + c2, v1 + v2)
        assert repo.set_published(scope, rev2, True) == 1
        assert repo.count_chunks(scope, rev1, published=True) == 0
        assert _ids(repo.search_keyword(scope, "plum", _filters(ws))) == [c2[0].chunk_id]

    # ------------------------------------------------------- publication gates the searches

    def test_unpublished_chunks_are_never_returned(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, pub, cp, vp = make_revision(scope, ws, ["quartz crystal lamp"], [E1])
        _d, unpub, cu, vu = make_revision(scope, ws, ["quartz quartz quartz crystal"], [E1])
        repo.upsert_chunks(scope, cp + cu, vp + vu)
        repo.set_published(scope, pub, True)
        f = _filters(ws)
        for hits in (
            repo.search_keyword(scope, "quartz", f),
            repo.search_vector(scope, SPACE, E1, f),
            repo.search_hybrid(scope, SPACE, "quartz", E1, f),
        ):
            assert _ids(hits) == [cp[0].chunk_id]
        assert unpub != pub

    # ------------------------------------------------------------------ cross-tenant

    def test_cross_tenant_isolation(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        a, b = new_scope(), new_scope()
        ws = uuid.uuid4()  # the SAME workspace id in both orgs: only the tenant separates them
        for s in (a, b):
            repo.ensure_indexes(s, SPACE, DIMS)
        _d, rev, chunks, vectors = make_revision(a, ws, ["secret falcon plan"], [E1])
        repo.upsert_chunks(a, chunks, vectors)
        repo.set_published(a, rev, True)
        f = _filters(ws)
        assert _ids(repo.search_keyword(a, "falcon", f)) == [chunks[0].chunk_id]

        # B cannot read, count, list, search, publish or delete A's chunks, even knowing the ids.
        assert repo.search_keyword(b, "falcon", f) == []
        assert repo.search_vector(b, SPACE, E1, f) == []
        assert repo.search_hybrid(b, SPACE, "falcon", E1, f) == []
        assert repo.count_chunks(b, rev) == 0
        assert repo.list_chunk_ids(b, rev) == []
        assert repo.set_published(b, rev, False) == 0
        assert repo.set_published(b, rev, True) == 0
        assert repo.delete_revision(b, rev).chunks == 0
        assert repo.delete_document(b, chunks[0].document_id).chunks == 0
        # ... and cannot write A's chunk objects into B.
        with pytest.raises(ValueError, match="different organization"):
            repo.upsert_chunks(b, chunks, vectors)
        # A is untouched.
        assert repo.count_chunks(a, rev, published=True) == 1
        assert _ids(repo.search_keyword(a, "falcon", f)) == [chunks[0].chunk_id]

    def test_same_chunk_id_in_two_tenants_stays_separate(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        a, b = new_scope(), new_scope()
        ws = uuid.uuid4()
        rev, doc, version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        ca = make_chunk(
            a, ws, text="tenant a words", revision_id=rev, document_id=doc, version_id=version
        )
        cb = make_chunk(
            b, ws, text="tenant b words", revision_id=rev, document_id=doc, version_id=version
        )
        assert ca.chunk_id == cb.chunk_id
        for s, c in ((a, ca), (b, cb)):
            repo.ensure_indexes(s, SPACE, DIMS)
            repo.upsert_chunks(s, [c], [E1])
            repo.set_published(s, rev, True)
        (hit_a,) = repo.search_keyword(a, "words", _filters(ws))
        (hit_b,) = repo.search_keyword(b, "words", _filters(ws))
        assert hit_a.text == "tenant a words"
        assert hit_b.text == "tenant b words"
        repo.delete_revision(a, rev)
        assert repo.count_chunks(a, rev) == 0
        assert repo.count_chunks(b, rev) == 1

    # ---------------------------------------------------------------- cross-workspace

    def test_cross_workspace_isolation_for_every_search(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws1, ws2 = new_scope(), uuid.uuid4(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        # ws1 holds weak matches; ws2 holds the BEST textual AND vector match of the whole tenant.
        _d, r1, c1, v1 = make_revision(
            scope, ws1, ["lantern keeper walks", "lantern oil store"], [E2, E3]
        )
        _d, r2, c2, v2 = make_revision(scope, ws2, ["lantern lantern lantern keeper"], [E1])
        repo.upsert_chunks(scope, c1 + c2, v1 + v2)
        repo.set_published(scope, r1, True)
        repo.set_published(scope, r2, True)

        only1 = _filters(ws1)
        k = repo.search_keyword(scope, "lantern lantern keeper", only1)
        v = repo.search_vector(scope, SPACE, E1, only1)
        h = repo.search_hybrid(scope, SPACE, "lantern lantern keeper", E1, only1)
        for hits in (k, v, h):
            assert hits, "ws1 has matches"
            assert {x.workspace_id for x in hits} == {ws1}
            assert c2[0].chunk_id not in _ids(hits)

        both = _filters(ws1, ws2)
        assert _ids(repo.search_keyword(scope, "lantern lantern keeper", both))[0] == c2[0].chunk_id
        assert _ids(repo.search_vector(scope, SPACE, E1, both))[0] == c2[0].chunk_id
        assert (
            _ids(repo.search_hybrid(scope, SPACE, "lantern lantern keeper", E1, both))[0]
            == c2[0].chunk_id
        )

        # A workspace with no chunks yields nothing (and never falls back to "all").
        nobody = _filters(uuid.uuid4())
        assert repo.search_keyword(scope, "lantern", nobody) == []
        assert repo.search_vector(scope, SPACE, E1, nobody) == []
        assert repo.search_hybrid(scope, SPACE, "lantern", E1, nobody) == []

    def test_empty_workspace_filter_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            SearchFilters(frozenset())

    # ----------------------------------------------------------------------- deletion

    def test_delete_revision_removes_only_that_revision(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        doc, r1, c1, v1 = make_revision(scope, ws, ["pear one", "pear two"], [E1, E2])
        _d, r2, c2, v2 = make_revision(scope, ws, ["pear three"], [E3], document_id=doc)
        repo.upsert_chunks(scope, c1 + c2, v1 + v2)
        repo.set_published(scope, r1, True)
        repo.set_published(scope, r2, True)
        counts = repo.delete_revision(scope, r1)
        assert (counts.chunks, counts.evidence) == (2, 0)
        assert repo.count_chunks(scope, r1) == 0
        assert repo.count_chunks(scope, r2) == 1
        f = _filters(ws)
        assert _ids(repo.search_keyword(scope, "pear", f)) == [c2[0].chunk_id]
        assert _ids(repo.search_vector(scope, SPACE, E1, f)) == [c2[0].chunk_id]
        assert repo.delete_revision(scope, r1).chunks == 0  # idempotent

    def test_delete_document_removes_all_its_revisions(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        doc, r1, c1, v1 = make_revision(scope, ws, ["fig one"], [E1])
        _d, r2, c2, v2 = make_revision(
            scope, ws, ["fig two", "fig three"], [E2, E3], document_id=doc
        )
        other_doc, r3, c3, v3 = make_revision(scope, ws, ["fig other"], [E4])
        repo.upsert_chunks(scope, c1 + c2 + c3, v1 + v2 + v3)
        for r in (r1, r2, r3):
            repo.set_published(scope, r, True)
        assert repo.delete_document(scope, doc).chunks == 3
        assert repo.count_chunks(scope, r1) == repo.count_chunks(scope, r2) == 0
        assert repo.count_chunks(scope, r3) == 1
        assert _ids(repo.search_keyword(scope, "fig", _filters(ws))) == [c3[0].chunk_id]
        assert repo.delete_document(scope, doc).chunks == 0  # idempotent
        assert other_doc != doc

    # -------------------------------------------------------------------- validation

    def test_vector_dimension_mismatch_is_rejected(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, _v = make_revision(scope, ws, ["short vector"], [E1])
        with pytest.raises(ValueError, match="dimensions"):
            repo.upsert_chunks(scope, chunks, [[1.0, 0.0, 0.0]])
        with pytest.raises(ValueError, match="dimensions"):
            repo.search_vector(scope, SPACE, [1.0, 0.0], _filters(ws))
        with pytest.raises(ValueError, match="dimensions"):
            repo.search_hybrid(scope, SPACE, "short", [1.0] * 9, _filters(ws))
        assert repo.count_chunks(scope, rev) == 0  # nothing was written

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_vectors_are_rejected(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory, bad: float
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, _v = make_revision(scope, ws, ["bad vector"], [E1])
        with pytest.raises(ValueError, match="finite"):
            repo.upsert_chunks(scope, chunks, [[1.0, bad, 0.0, 0.0]])
        with pytest.raises(ValueError, match="finite"):
            repo.search_vector(scope, SPACE, [1.0, bad, 0.0, 0.0], _filters(ws))
        assert repo.count_chunks(scope, rev) == 0

    def test_mismatched_batch_and_mixed_spaces_are_rejected(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, _r, chunks, _v = make_revision(scope, ws, ["a b", "c d"], [E1, E2])
        with pytest.raises(ValueError, match="same length"):
            repo.upsert_chunks(scope, chunks, [E1])
        _d, _r, other, _v = make_revision(scope, ws, ["e f"], [E1], space="eother00000001")
        with pytest.raises(ValueError, match="share one embedding space"):
            repo.upsert_chunks(scope, [chunks[0], other[0]], [E1, E2])
        assert repo.upsert_chunks(scope, [], []) == 0

    def test_search_requires_ensured_indexes(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        _d, _r, chunks, vectors = make_revision(scope, ws, ["no index yet"], [E1])
        with pytest.raises(KnowledgeStoreError):
            repo.upsert_chunks(scope, chunks, vectors)
        with pytest.raises(KnowledgeStoreError):
            repo.search_vector(scope, SPACE, E1, _filters(ws))

    def test_ensure_indexes_is_idempotent_and_dimensions_are_immutable(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope = new_scope()
        repo.ensure_indexes(scope, SPACE, DIMS)
        repo.ensure_indexes(scope, SPACE, DIMS)
        with pytest.raises(KnowledgeStoreError):
            repo.ensure_indexes(scope, SPACE, DIMS + 1)
        repo.ensure_indexes(scope, "eanother00001", 8)  # a second space coexists

    def test_limits_and_queries_are_validated(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        f = _filters(ws)
        for bad in (0, -1, 201):
            with pytest.raises(ValueError, match="limit"):
                repo.search_keyword(scope, "x", f, limit=bad)
        with pytest.raises(ValueError, match="empty"):
            repo.search_keyword(scope, "   ", f)

    def test_hostile_query_text_is_data_not_syntax(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, vectors = make_revision(scope, ws, ["ordinary words here"], [E1])
        repo.upsert_chunks(scope, chunks, vectors)
        repo.set_published(scope, rev, True)
        for query in [
            '" OR 1=1 --',
            "words) OR (workspace_id:*",
            "{$ne: null}",
            "a:b OR c*",
            "\\",
            "!!!",
        ]:
            hits = repo.search_keyword(scope, query, _filters(uuid.uuid4()))
            assert hits == []  # never leaks through another workspace
        assert _ids(repo.search_keyword(scope, "ordinary! (words)", _filters(ws))) == [
            chunks[0].chunk_id
        ]

    # ---------------------------------------------------------------- ranking contracts

    def test_vector_search_ranks_by_cosine(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, vectors = make_revision(
            scope, ws, ["w x", "y z", "p q"], [E1, [0.9, 0.1, 0.0, 0.0], E3]
        )
        repo.upsert_chunks(scope, chunks, vectors)
        repo.set_published(scope, rev, True)
        hits = repo.search_vector(scope, SPACE, E1, _filters(ws), limit=3)
        assert _ids(hits) == [chunks[0].chunk_id, chunks[1].chunk_id, chunks[2].chunk_id]
        assert hits[0].score == pytest.approx(1.0, abs=1e-4)
        assert hits[0].score > hits[1].score > hits[2].score
        assert len(repo.search_vector(scope, SPACE, E1, _filters(ws), limit=2)) == 2

    def test_keyword_search_ranks_by_relevance(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        _d, rev, chunks, vectors = make_revision(
            scope,
            ws,
            [
                "tide tables harbor harbor harbor",
                "tide tables for the whole coast and many other places",
                "unrelated text",
            ],
            [E1, E2, E3],
        )
        repo.upsert_chunks(scope, chunks, vectors)
        repo.set_published(scope, rev, True)
        hits = repo.search_keyword(scope, "harbor tide", _filters(ws))
        assert _ids(hits) == [chunks[0].chunk_id, chunks[1].chunk_id]
        assert hits[0].score > hits[1].score > 0

    def test_hybrid_fuses_both_legs_by_rrf(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        scope, ws = new_scope(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        # both: best vector AND a text match; text_only: only text; vec_only: only vector.
        _d, rev, chunks, vectors = make_revision(
            scope,
            ws,
            ["meridian clock tower", "meridian meridian meridian", "something else entirely"],
            [E1, E4, [0.8, 0.2, 0.0, 0.0]],
        )
        repo.upsert_chunks(scope, chunks, vectors)
        repo.set_published(scope, rev, True)
        hits = repo.search_hybrid(scope, SPACE, "meridian", E1, _filters(ws), limit=3)
        assert _ids(hits)[0] == chunks[0].chunk_id  # in both legs
        assert set(_ids(hits)) == {c.chunk_id for c in chunks}
        assert hits[0].score == pytest.approx(1 / 61 + 1 / 62, abs=1e-6)
        assert all(a.score >= b.score for a, b in pairwise(hits))

    def test_filter_before_rank_returns_a_full_page(
        self, repo: KnowledgeRepository, new_scope: ScopeFactory
    ) -> None:
        """Closer unauthorized chunks must not starve the page of authorized ones."""
        scope, ws_ok, ws_no = new_scope(), uuid.uuid4(), uuid.uuid4()
        repo.ensure_indexes(scope, SPACE, DIMS)
        n = 60
        near = [[1.0, 0.001 * i, 0.0, 0.0] for i in range(n)]
        far = [[0.2, 1.0, 0.001 * i, 0.0] for i in range(n)]
        _d, r_no, c_no, v_no = make_revision(
            scope, ws_no, [f"gadget unauthorized {i}" for i in range(n)], near
        )
        _d, r_ok, c_ok, v_ok = make_revision(
            scope, ws_ok, [f"gadget authorized {i}" for i in range(n)], far
        )
        repo.upsert_chunks(scope, c_no + c_ok, v_no + v_ok)
        repo.set_published(scope, r_no, True)
        repo.set_published(scope, r_ok, True)
        f = _filters(ws_ok)
        for hits in (
            repo.search_vector(scope, SPACE, E1, f, limit=20),
            repo.search_keyword(scope, "gadget", f, limit=20),
            repo.search_hybrid(scope, SPACE, "gadget", E1, f, limit=20),
        ):
            assert len(hits) == 20
            assert {h.workspace_id for h in hits} == {ws_ok}
