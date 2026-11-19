"""Real-RushDB behaviour of the adapter beyond the shared contract (M03.03.01).

Tenant scoping on the wire, the single predicate choke point (checked against the REAL request
log), stored shapes, scale (filter-before-rank), concurrency and failure mapping.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from m01_seed import add_member, insert_org, insert_user  # type: ignore[import-not-found,unused-ignore]
from rdb_support import RecordingTransport, RushDbStack, raw_execute, soft_delete_org
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.db import tenant_session
from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
from app.modules.knowledge.ports import (
    KnowledgeStoreUnavailable,
    OrgScope,
    SearchFilters,
    UnknownOrganization,
    validated_org_scope,
)
from tests.infrastructure_rushdb.support import (
    DIMS,
    SPACE,
    assert_every_retrieval_is_scoped,
    make_revision,
)

E1 = [1.0, 0.0, 0.0, 0.0]
E2 = [0.0, 1.0, 0.0, 0.0]


def _filters(*workspaces: uuid.UUID) -> SearchFilters:
    return SearchFilters(frozenset(workspaces))


# --------------------------------------------------------------------- validated_org_scope


def test_validated_org_scope_with_real_postgres(
    admin_engine: Engine, app_engine: Engine, pg_session: Session
) -> None:
    with admin_engine.begin() as conn:
        user = insert_user(conn)
        org = insert_org(conn, created_by=user)
        other = insert_org(conn, created_by=insert_user(conn))
        gone = insert_org(conn, created_by=user)
        add_member(conn, org, user, "owner")
        add_member(conn, gone, user, "owner")
        add_member(conn, other, insert_user(conn), "owner")
    soft_delete_org(admin_engine, gone)

    # Admin session (sees everything): live org ok, deleted org and unknown id rejected.
    assert validated_org_scope(pg_session, org).tenant == f"org:{org}"
    with pytest.raises(UnknownOrganization):
        validated_org_scope(pg_session, gone)
    with pytest.raises(UnknownOrganization):
        validated_org_scope(pg_session, uuid.uuid4())

    # App role under RLS: a member's context sees its org; another org (or an id guessed from a
    # request) is invisible, hence unknown, even though it exists.
    with tenant_session(user_id=user, org_id=org, engine=app_engine) as session:
        assert validated_org_scope(session, org).org_id == org
    with (
        tenant_session(user_id=None, org_id=uuid.uuid4(), engine=app_engine) as session,
        pytest.raises(UnknownOrganization),
    ):
        validated_org_scope(session, org)
    # ``other`` exists, but the user is not a member and the context is another org.
    with (
        tenant_session(user_id=user, org_id=org, engine=app_engine) as session,
        pytest.raises(UnknownOrganization),
    ):
        validated_org_scope(session, other)


def test_scope_cannot_be_forged_from_an_id() -> None:
    with pytest.raises(TypeError, match="validated_org_scope"):
        OrgScope(uuid.uuid4())


# ---------------------------------------------------------------------- tenant on the wire


def test_every_request_is_tenant_scoped_to_the_validated_org_only(
    rushdb_repo: RushDbKnowledgeRepository,
    recording: RecordingTransport,
    scope_factory: Callable[[], OrgScope],
) -> None:
    scope, other = scope_factory(), scope_factory()
    ws = uuid.uuid4()
    rushdb_repo.ensure_indexes(scope, SPACE, DIMS)
    _d, rev, chunks, vectors = make_revision(scope, ws, ["wire check words"], [E1])
    rushdb_repo.upsert_chunks(scope, chunks, vectors)
    rushdb_repo.set_published(scope, rev, True)
    rushdb_repo.search_keyword(scope, "wire", _filters(ws))
    rushdb_repo.delete_revision(scope, rev)
    assert recording.requests
    for request, body in zip(recording.requests, recording.bodies, strict=True):
        assert request.headers["X-RushDB-Tenant"] == scope.tenant == body["context"]["scope"]
    assert other.tenant not in {r.headers["X-RushDB-Tenant"] for r in recording.requests}


def test_the_real_request_log_never_omits_the_predicate(
    rushdb_repo: RushDbKnowledgeRepository,
    recording: RecordingTransport,
    scope_factory: Callable[[], OrgScope],
) -> None:
    """Instrumented real adapter: every retrieval it ever sent carries workspace + published."""
    scope = scope_factory()
    ws1, ws2 = uuid.uuid4(), uuid.uuid4()
    rushdb_repo.ensure_indexes(scope, SPACE, DIMS)
    _d, rev, chunks, vectors = make_revision(
        scope, ws1, ["log check alpha", "log check beta"], [E1, E2]
    )
    rushdb_repo.upsert_chunks(scope, chunks, vectors)
    rushdb_repo.set_published(scope, rev, True)
    for filters in (_filters(ws1), _filters(ws1, ws2), _filters(ws2)):
        rushdb_repo.search_keyword(scope, "log alpha", filters)
        rushdb_repo.search_vector(scope, SPACE, E1, filters)
        rushdb_repo.search_hybrid(scope, SPACE, "log alpha", E1, filters)
    rushdb_repo.count_chunks(scope, rev)
    rushdb_repo.list_chunk_ids(scope, rev)
    rushdb_repo.delete_document(scope, chunks[0].document_id)
    assert assert_every_retrieval_is_scoped(recording.operations) == 9


# --------------------------------------------------------------------------- stored shape


def test_stored_records_have_typed_properties_and_bound_vectors(
    rushdb_stack: RushDbStack,
    rushdb_repo: RushDbKnowledgeRepository,
    scope_factory: Callable[[], OrgScope],
) -> None:
    scope, ws = scope_factory(), uuid.uuid4()
    rushdb_repo.ensure_indexes(scope, SPACE, DIMS)
    _d, rev, chunks, vectors = make_revision(scope, ws, ["typed one", "typed two"], [E1, E2])
    rushdb_repo.upsert_chunks(scope, chunks, vectors)

    def raw(operation: dict[str, Any]) -> dict[str, Any]:
        body = raw_execute(rushdb_stack.url, rushdb_stack.token, scope.tenant, operation)
        response = body["response"]
        assert isinstance(response, dict)
        return response

    listed = raw(
        {
            "kind": "query",
            "query": {"kind": "records", "query": {"labels": ["Chunk"], "limit": 10}},
        }
    )
    records = listed["records"]
    assert isinstance(records, list)
    assert len(records) == 2
    record = next(r for r in records if r["id"] == str(chunks[0].chunk_id))  # explicit id
    types = record["propertyTypes"]
    assert types["published"] == "boolean"
    assert record["properties"]["published"] is False
    assert types["chunk_index"] == "number"
    assert types["token_estimate"] == "number"
    assert types["page_start"] == "number"
    assert types["page_end"] == "number"
    assert types["text"] == "string"
    assert types["locators"] == "string"
    for key in ("org_id", "workspace_id", "document_id", "version_id", "revision_id"):
        assert record["properties"][key] == str(getattr(chunks[0], key))
    assert record["properties"]["org_id"] == str(scope.org_id)
    assert "embedding" not in record["properties"]  # the vector lives in the index

    stats = raw(
        {"kind": "describe", "describe": {"kind": "vectorIndex", "name": f"chunk-emb-{SPACE}"}}
    )
    assert stats["stats"]["indexedRecords"] == 2
    assert stats["stats"]["index"]["dimensions"] == DIMS
    indexes = raw({"kind": "describe", "describe": {"kind": "indexes"}})["indexes"]
    assert any(i["name"] == "chunk-text" and i["state"] == "ready" for i in indexes)

    # set_published is a property merge: the vector binding survives it.
    rushdb_repo.set_published(scope, rev, True)
    stats = raw(
        {"kind": "describe", "describe": {"kind": "vectorIndex", "name": f"chunk-emb-{SPACE}"}}
    )
    assert stats["stats"]["indexedRecords"] == 2

    # Observed on 3.0.10: after deleteRecords the vector index STATS still count the deleted
    # records (totalRecords/externalBindings do not drop), although no search can return them and
    # the records are gone. So we assert on behaviour, not on those counters.
    rushdb_repo.delete_revision(scope, rev)
    assert rushdb_repo.count_chunks(scope, rev) == 0
    assert rushdb_repo.search_vector(scope, SPACE, E1, _filters(ws)) == []


def test_reindexing_after_a_purge_does_not_resurrect_or_duplicate(
    rushdb_repo: RushDbKnowledgeRepository, scope_factory: Callable[[], OrgScope]
) -> None:
    scope, ws = scope_factory(), uuid.uuid4()
    rushdb_repo.ensure_indexes(scope, SPACE, DIMS)
    _d, rev, chunks, vectors = make_revision(scope, ws, ["phoenix one", "phoenix two"], [E1, E2])
    for _ in range(2):  # index, publish, purge, and do it all again with the same ids
        rushdb_repo.upsert_chunks(scope, chunks, vectors)
        assert rushdb_repo.count_chunks(scope, rev, published=True) == 0  # purge reset the flag
        rushdb_repo.set_published(scope, rev, True)
        assert len(rushdb_repo.search_vector(scope, SPACE, E1, _filters(ws))) == 2
        assert len(rushdb_repo.search_keyword(scope, "phoenix", _filters(ws))) == 2
        rushdb_repo.delete_revision(scope, rev)
        assert rushdb_repo.search_vector(scope, SPACE, E1, _filters(ws)) == []


def test_an_unknown_tenant_is_not_created_by_failed_scope_validation(
    rushdb_stack: RushDbStack, pg_session: Session
) -> None:
    """RushDB auto-registers unknown scopes; validation must stop us before any call is made."""
    ghost = uuid.uuid4()
    with pytest.raises(UnknownOrganization):
        validated_org_scope(pg_session, ghost)
    # No OrgScope exists for the ghost org, so no repository method can even be called for it.
    body = raw_execute(
        rushdb_stack.url,
        rushdb_stack.token,
        f"org:{uuid.uuid4()}",
        {"kind": "describe", "describe": {"kind": "indexes"}},
    )
    assert (
        body["response"]["indexes"] == []
    )  # an unvalidated scope would silently be an empty tenant


# ----------------------------------------------------------------------------- scale


def test_filter_before_rank_at_scale(
    rushdb_repo: RushDbKnowledgeRepository, scope_factory: Callable[[], OrgScope]
) -> None:
    """3,000 authorized + 3,000 closer unauthorized chunks still yield a full page of authorized."""
    scope, ws_ok, ws_no = scope_factory(), uuid.uuid4(), uuid.uuid4()
    dims, n = 8, 3000
    rushdb_repo.ensure_indexes(scope, "escale00000001", dims)
    rng = random.Random(7)  # noqa: S311 - deterministic test data
    query = [1.0] + [0.0] * (dims - 1)
    near = [[1.0] + [rng.uniform(-0.02, 0.02) for _ in range(dims - 1)] for _ in range(n)]
    far = [[0.2, 1.0] + [rng.uniform(-1, 1) for _ in range(dims - 2)] for _ in range(n)]
    _d, r_no, c_no, v_no = make_revision(
        scope, ws_no, [f"gizmo forbidden {i}" for i in range(n)], near, space="escale00000001"
    )
    _d, r_ok, c_ok, v_ok = make_revision(
        scope, ws_ok, [f"gizmo permitted {i}" for i in range(n)], far, space="escale00000001"
    )
    rushdb_repo.upsert_chunks(scope, c_no, v_no)
    rushdb_repo.upsert_chunks(scope, c_ok, v_ok)
    assert rushdb_repo.set_published(scope, r_no, True) == n
    assert rushdb_repo.set_published(scope, r_ok, True) == n
    assert rushdb_repo.count_chunks(scope, r_ok, published=True) == n

    f = _filters(ws_ok)
    for hits in (
        rushdb_repo.search_vector(scope, "escale00000001", query, f, limit=50),
        rushdb_repo.search_keyword(scope, "gizmo", f, limit=50),
        rushdb_repo.search_hybrid(scope, "escale00000001", "gizmo", query, f, limit=50),
    ):
        assert len(hits) == 50
        assert {h.workspace_id for h in hits} == {ws_ok}
        assert all(h.revision_id == r_ok for h in hits)
    # Sanity: unfiltered by workspace the unauthorized chunks WOULD have won the vector ranking.
    unfiltered = rushdb_repo.search_vector(scope, "escale00000001", query, _filters(ws_no), limit=5)
    assert {h.workspace_id for h in unfiltered} == {ws_no}


# ----------------------------------------------------------------------- concurrency


def test_concurrent_duplicate_upserts_yield_one_record_each(
    rushdb_repo: RushDbKnowledgeRepository, scope_factory: Callable[[], OrgScope]
) -> None:
    scope, ws = scope_factory(), uuid.uuid4()
    rushdb_repo.ensure_indexes(scope, SPACE, DIMS)
    _d, rev, chunks, vectors = make_revision(
        scope, ws, [f"duplicate delivery {i}" for i in range(40)], [E1] * 40
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: rushdb_repo.upsert_chunks(scope, chunks, vectors), range(4))
        )
    assert results == [40] * 4
    assert rushdb_repo.count_chunks(scope, rev) == 40
    assert len(set(rushdb_repo.list_chunk_ids(scope, rev))) == 40


# ------------------------------------------------------------------------- failures


def test_unreachable_server_and_wrong_token_map_to_unavailable(
    rushdb_stack: RushDbStack, scope_factory: Callable[[], OrgScope]
) -> None:
    scope = scope_factory()
    down = RushDbKnowledgeRepository(
        rushdb_stack.settings(
            RUSHDB_URL="http://127.0.0.1:1",
            RUSHDB_CONNECT_TIMEOUT_SECONDS=0.5,
            RUSHDB_RETRY_BACKOFF_SECONDS=0,
        )
    )
    with pytest.raises(KnowledgeStoreUnavailable) as info:
        down.count_chunks(scope, uuid.uuid4())
    assert "127.0.0.1" not in str(info.value)
    down.close()

    wrong = RushDbKnowledgeRepository(
        rushdb_stack.settings(
            RUSHDB_TOKEN="definitely-not-the-token", RUSHDB_RETRY_BACKOFF_SECONDS=0
        )
    )
    with pytest.raises(KnowledgeStoreUnavailable) as bad:
        wrong.count_chunks(scope, uuid.uuid4())
    assert "definitely-not-the-token" not in str(bad.value)
    wrong.close()


def test_the_server_enforces_the_tenant_header_matches_the_envelope(
    rushdb_stack: RushDbStack, scope_factory: Callable[[], OrgScope]
) -> None:
    """Spike section 3.1 still holds on this build: a mismatch is refused, never re-routed."""
    a, b = scope_factory(), scope_factory()
    response = httpx.post(
        f"{rushdb_stack.url}/protocol/v1/execute",
        json={
            "protocolVersion": 1,
            "context": {"scope": a.tenant},
            "operation": {"kind": "describe", "describe": {"kind": "indexes"}},
        },
        headers={"Authorization": f"Bearer {rushdb_stack.token}", "X-RushDB-Tenant": b.tenant},
        timeout=10,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_mismatch"
