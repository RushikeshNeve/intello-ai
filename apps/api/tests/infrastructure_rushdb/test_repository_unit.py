"""Wire-level unit tests of ``RushDbKnowledgeRepository`` against a fake HTTP transport.

They pin the security properties that must never regress: tenant headers, the single predicate
choke point (a test fails if any search omits the workspace or published predicate), forbidden
query kinds, error mapping, bounded retries, and no secrets / chunk text in exceptions or logs.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

import app.infrastructure.rushdb.repository as repository_module
from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
from app.infrastructure.rushdb.settings import RushDbSettings
from app.modules.knowledge.ports import (
    KnowledgeRepository,
    KnowledgeStoreError,
    KnowledgeStoreUnavailable,
    OrgScope,
    SearchFilters,
)
from tests.infrastructure_rushdb.support import (
    DIMS,
    SPACE,
    assert_every_retrieval_is_scoped,
    fake_scope,
    make_chunk,
    make_revision,
)

TOKEN = "tok-very-secret-1234"
HOST = "rushdb.internal.example"
MARKER = "CHUNK-TEXT-MARKER-9f3a"


class FakeRushDb:
    """A recording fake of the Protocol v1 endpoint; behaviour is scripted per test."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.published_ids: list[str] = []
        self.text_index_states: list[str | None] = ["ready"]
        self.counts: list[int] = [0]
        self.fail_with: list[httpx.Response | Exception] = []
        self.dims = DIMS

    @property
    def operations(self) -> list[dict[str, Any]]:
        return [b["operation"] for b in self.bodies]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.fail_with:
            failure = self.fail_with.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        return httpx.Response(
            200, json={"protocolVersion": 1, "response": self._respond(body["operation"])}
        )

    def _respond(self, op: dict[str, Any]) -> dict[str, Any]:
        kind = op["kind"]
        if kind == "index":
            return {"kind": "index", "index": {}}
        if kind == "describe":
            if op["describe"]["kind"] == "vectorIndex":
                return {"kind": "vectorIndexStats", "stats": {"index": {"dimensions": self.dims}}}
            state = (
                self.text_index_states.pop(0)
                if len(self.text_index_states) > 1
                else self.text_index_states[0]
            )
            indexes = [] if state is None else [{"name": "chunk-text", "state": state}]
            return {"kind": "indexes", "indexes": indexes}
        if kind == "mutate":
            m = op["mutation"]
            if m["kind"] == "deleteRecords":
                return {"kind": "mutation", "affected": 2}
            return {"kind": "record", "record": {"id": m.get("id")}}
        q = op["query"]
        if q["kind"] == "records":
            query = q["query"]
            if "$count" in json.dumps(query.get("select", {})):
                n = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
                return {"kind": "rows", "rows": [{"n": n}]}
            if "$id" in query.get("predicate", {}):
                return {"kind": "rows", "rows": [{"chunk_id": c} for c in self.published_ids]}
            return {"kind": "rows", "rows": []}
        return {"kind": "records", "records": []}


def make_repo(
    fake: FakeRushDb, *, attempts: int = 3, batch: int = 100
) -> tuple[RushDbKnowledgeRepository, list[float]]:
    sleeps: list[float] = []
    settings = RushDbSettings(
        RUSHDB_URL=f"http://{HOST}:3000",
        RUSHDB_TOKEN=SecretStr(TOKEN),
        RUSHDB_MAX_ATTEMPTS=attempts,
        RUSHDB_BATCH_SIZE=batch,
        RUSHDB_RETRY_BACKOFF_SECONDS=0.01,
        RUSHDB_INDEX_READY_TIMEOUT_SECONDS=5,
    )
    client = httpx.Client(base_url=settings.RUSHDB_URL, transport=httpx.MockTransport(fake.handler))
    return RushDbKnowledgeRepository(settings, client=client, sleep=sleeps.append), sleeps


@pytest.fixture
def fake() -> FakeRushDb:
    return FakeRushDb()


@pytest.fixture
def scope() -> OrgScope:
    return fake_scope()


def test_every_request_carries_the_tenant_twice_and_the_token(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    repo.count_chunks(scope, uuid.uuid4())
    (request,) = fake.requests
    assert request.url.path == "/protocol/v1/execute"
    assert request.method == "POST"
    assert request.headers["X-RushDB-Tenant"] == f"org:{scope.org_id}"
    assert fake.bodies[0]["context"] == {"scope": f"org:{scope.org_id}"}
    assert fake.bodies[0]["protocolVersion"] == 1
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


# ------------------------------------------------------------------- predicate choke point


def _exercise_every_method(repo: KnowledgeRepository, scope: OrgScope) -> set[str]:
    """Call every public method of the port once; return the names exercised."""
    ws1, ws2 = uuid.uuid4(), uuid.uuid4()
    filters = SearchFilters(frozenset({ws1, ws2}))
    _d, rev, chunks, vectors = make_revision(scope, ws1, ["a b c"], [[1.0, 0.0, 0.0, 0.0]])
    repo.ensure_indexes(scope, SPACE, DIMS)
    repo.upsert_chunks(scope, chunks, vectors)
    repo.count_chunks(scope, rev)
    repo.list_chunk_ids(scope, rev)
    repo.set_published(scope, rev, True)
    repo.delete_revision(scope, rev)
    repo.delete_document(scope, chunks[0].document_id)
    repo.search_keyword(scope, "a b", filters)
    repo.search_vector(scope, SPACE, [1.0, 0.0, 0.0, 0.0], filters)
    repo.search_hybrid(scope, SPACE, "a b", [1.0, 0.0, 0.0, 0.0], filters)
    return {
        "ensure_indexes", "upsert_chunks", "count_chunks", "list_chunk_ids", "set_published",
        "delete_revision", "delete_document", "search_keyword", "search_vector", "search_hybrid",
    }  # fmt: skip


def test_every_public_method_is_covered_by_the_scope_check(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    exercised = _exercise_every_method(repo, scope)
    public = {
        name
        for name, member in inspect.getmembers(RushDbKnowledgeRepository, inspect.isfunction)
        if not name.startswith("_") and name != "close"
    }
    assert public == exercised, "a new public method must be added to the scope-check exercise"


def test_every_search_injects_workspace_and_published_predicate(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    ws1, ws2 = uuid.uuid4(), uuid.uuid4()
    filters = SearchFilters(frozenset({ws1, ws2}))
    vec = [1.0, 0.0, 0.0, 0.0]
    repo.search_keyword(scope, "lighthouse", filters)
    repo.search_vector(scope, SPACE, vec, filters)
    repo.search_hybrid(scope, SPACE, "lighthouse", vec, filters)
    searches = [
        o
        for o in fake.operations
        if o["kind"] == "query" and o["query"]["kind"] in {"hybrid", "vector"}
    ]
    assert len(searches) == 3
    assert assert_every_retrieval_is_scoped(fake.operations, {str(ws1), str(ws2)}) == 3
    keyword, vector, hybrid = (s["query"] for s in searches)
    # keyword = hybrid with ONLY a text leg; the others add the vector leg / use the space index
    assert keyword["kind"] == "hybrid"
    assert "vector" not in keyword["query"]
    assert "text" in keyword["query"]
    assert vector["kind"] == "vector"
    assert vector["query"]["index"] == f"chunk-emb-{SPACE}"
    assert hybrid["kind"] == "hybrid"
    assert hybrid["query"]["fusion"] == "rrf"
    assert hybrid["query"]["rrfK"] == 60
    assert hybrid["query"]["vector"]["index"] == f"chunk-emb-{SPACE}"
    assert vector["query"]["predicate"]["embedding_space_id"] == SPACE
    assert "embedding_space_id" not in keyword["query"]["predicate"]


def test_full_exercise_issues_only_allowed_operations(fake: FakeRushDb, scope: OrgScope) -> None:
    repo, _ = make_repo(fake)
    _exercise_every_method(repo, scope)
    kinds = set()
    for op in fake.operations:
        kinds.add(op["kind"] if op["kind"] != "query" else f"query.{op['query']['kind']}")
        if op["kind"] == "mutate":
            assert op["mutation"]["kind"] in {"putRecord", "deleteRecords"}
            assert op["mutation"].get("options", {}) == {}  # never suggestTypes:false
    assert kinds <= {"index", "describe", "mutate", "query.records", "query.hybrid", "query.vector"}
    assert not any("suggestTypes" in json.dumps(b) for b in fake.bodies)
    assert not any("statement" in json.dumps(op) for op in fake.operations)


def test_the_checker_fails_when_a_predicate_is_dropped(
    fake: FakeRushDb, scope: OrgScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation check: if the choke point stops adding the predicate, the guard test must fail."""
    repo, _ = make_repo(fake)
    filters = SearchFilters(frozenset({uuid.uuid4()}))
    monkeypatch.setattr(repository_module, "_scoped_predicate", lambda f, **kw: {"published": True})
    repo.search_keyword(scope, "x", filters)
    with pytest.raises(KeyError):  # workspace predicate missing
        assert_every_retrieval_is_scoped(fake.operations, {str(next(iter(filters.workspace_ids)))})
    fake.bodies.clear()
    monkeypatch.setattr(
        repository_module, "_scoped_predicate", lambda f, **kw: {"workspace_id": {"$in": ["w"]}}
    )
    repo.search_keyword(scope, "x", filters)
    with pytest.raises(KeyError):  # published predicate missing
        assert_every_retrieval_is_scoped(fake.operations, {"w"})


def test_search_rejects_non_filters_and_bad_arguments_before_any_request(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    with pytest.raises(TypeError):
        repo.search_keyword(scope, "x", None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        repo.search_keyword(scope, "x", {"workspace_id": "w"})  # type: ignore[arg-type]
    filters = SearchFilters(frozenset({uuid.uuid4()}))
    with pytest.raises(ValueError, match="limit"):
        repo.search_keyword(scope, "x", filters, limit=0)
    with pytest.raises(ValueError, match="empty"):
        repo.search_keyword(scope, " ", filters)
    with pytest.raises(ValueError, match="embedding_space_id"):
        repo.search_vector(scope, "bad space", [1.0], filters)
    assert fake.requests == []


def test_hostile_query_text_is_sent_as_a_json_string_only(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    hostile = 'x"}},"predicate":{"$or":[{}]}}//'
    repo.search_keyword(scope, hostile, SearchFilters(frozenset({uuid.uuid4()})))
    op = fake.operations[0]["query"]["query"]
    assert op["text"]["query"] == hostile
    assert set(op) == {"labels", "limit", "predicate", "text"}
    assert set(op["predicate"]) == {"workspace_id", "published"}


# --------------------------------------------------------------------------------- indexes


def test_ensure_indexes_creates_both_and_polls_until_ready(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    fake.text_index_states = [None, "building", "building", "ready"]
    repo, sleeps = make_repo(fake)
    repo.ensure_indexes(scope, SPACE, DIMS)
    ops = fake.operations
    vector = ops[0]["index"]
    assert vector["kind"] == "createVector"
    assert vector["definition"] == {
        "name": f"chunk-emb-{SPACE}", "label": "Chunk", "property": "embedding",
        "dimensions": DIMS, "similarity": "cosine", "model": SPACE,
    }  # fmt: skip
    text_create = next(o for o in ops if o["kind"] == "index" and o["index"]["kind"] == "create")
    assert text_create["index"]["definition"]["name"] == "chunk-text"
    assert text_create["index"]["definition"]["consistency"] == "immediate"
    assert len(sleeps) >= 2  # polled while building


def test_ensure_indexes_skips_text_creation_when_present_and_tolerates_a_race(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    repo.ensure_indexes(scope, SPACE, DIMS)
    assert not any(o["kind"] == "index" and o["index"]["kind"] == "create" for o in fake.operations)

    racing = FakeRushDb()
    racing.text_index_states = [None, "ready"]
    original = racing._respond

    def respond(op: dict[str, Any]) -> dict[str, Any]:
        if op["kind"] == "index" and op["index"]["kind"] == "create":
            raise _Conflict
        return original(op)

    class _Conflict(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        try:
            return httpx.Response(200, json={"response": respond(body["operation"])})
        except _Conflict:
            return httpx.Response(409, json={"error": {"code": "conflict", "message": "exists"}})

    settings = RushDbSettings(RUSHDB_URL="http://x:1", RUSHDB_TOKEN=SecretStr(TOKEN))
    repo2 = RushDbKnowledgeRepository(
        settings,
        client=httpx.Client(base_url="http://x:1", transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
    )
    repo2.ensure_indexes(scope, SPACE, DIMS)  # conflict on the text index is fine


def test_ensure_indexes_times_out_when_the_text_index_never_gets_ready(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    fake.text_index_states = ["building"]
    settings = RushDbSettings(
        RUSHDB_TOKEN=SecretStr(TOKEN), RUSHDB_INDEX_READY_TIMEOUT_SECONDS=0.05
    )
    repo = RushDbKnowledgeRepository(
        settings,
        client=httpx.Client(base_url="http://x:1", transport=httpx.MockTransport(fake.handler)),
        sleep=lambda _s: None,
    )
    with pytest.raises(KnowledgeStoreUnavailable, match="not ready"):
        repo.ensure_indexes(scope, SPACE, DIMS)


def test_ensure_indexes_validates_arguments(fake: FakeRushDb, scope: OrgScope) -> None:
    repo, _ = make_repo(fake)
    with pytest.raises(ValueError, match="dimensions"):
        repo.ensure_indexes(scope, SPACE, 0)
    with pytest.raises(ValueError, match="embedding_space_id"):
        repo.ensure_indexes(scope, "bad space", 4)
    assert fake.requests == []


# ---------------------------------------------------------------------------------- upsert


def test_upsert_batches_of_at_most_100_and_validates_everything_first(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    ws = uuid.uuid4()
    texts = [f"chunk number {i}" for i in range(250)]
    vectors = [[1.0, 0.0, 0.0, 0.0]] * 250
    _d, _r, chunks, _v = make_revision(scope, ws, texts, vectors)

    bad = list(vectors)
    bad[249] = [1.0, 0.0, 0.0]  # the very last vector is wrong: nothing may be written
    with pytest.raises(ValueError, match="dimensions"):
        repo.upsert_chunks(scope, chunks, bad)
    assert not any(o["kind"] == "mutate" for o in fake.operations)

    assert repo.upsert_chunks(scope, chunks, vectors) == 250
    puts = [o["mutation"] for o in fake.operations if o["kind"] == "mutate"]
    lookups = [
        o for o in fake.operations
        if o["kind"] == "query" and "$id" in o["query"]["query"].get("predicate", {})
    ]  # fmt: skip
    assert len(puts) == 250
    assert [len(o["query"]["query"]["predicate"]["$id"]["$in"]) for o in lookups] == [100, 100, 50]
    first = puts[0]
    assert first["mode"] == "merge"
    assert first["label"] == "Chunk"
    assert first["options"] == {}
    assert first["id"] == str(chunks[0].chunk_id)
    assert first["vectors"] == [
        {"propertyName": "embedding", "vector": [1.0, 0.0, 0.0, 0.0], "index": f"chunk-emb-{SPACE}"}
    ]
    props = first["properties"]
    assert props["published"] is False
    assert props["text"] == "chunk number 0"
    assert props["org_id"] == str(scope.org_id)
    assert props["workspace_id"] == str(ws)
    assert isinstance(props["chunk_index"], int)
    assert isinstance(props["token_estimate"], int)
    assert json.loads(props["locators"]) == [{"type": "page", "page": 1}]
    assert json.loads(props["heading_path"]) == ["Intro"]
    assert props["page_start"] == 1
    assert props["page_end"] == 1
    assert props["embedding_space_id"] == SPACE


def test_upsert_does_not_unpublish_an_already_published_chunk(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    _d, _r, chunks, vectors = make_revision(
        scope, uuid.uuid4(), ["x y", "z w"], [[1.0, 0, 0, 0]] * 2
    )
    fake.published_ids = [str(chunks[0].chunk_id)]
    repo.upsert_chunks(scope, chunks, vectors)
    puts = {o["mutation"]["id"]: o["mutation"] for o in fake.operations if o["kind"] == "mutate"}
    assert "published" not in puts[str(chunks[0].chunk_id)]["properties"]
    assert puts[str(chunks[1].chunk_id)]["properties"]["published"] is False


def test_upsert_omits_pages_when_unknown(fake: FakeRushDb, scope: OrgScope) -> None:
    repo, _ = make_repo(fake)
    chunk = make_chunk(scope, uuid.uuid4(), text="no pages here", page=None)
    repo.upsert_chunks(scope, [chunk], [[1.0, 0, 0, 0]])
    props = next(o for o in fake.operations if o["kind"] == "mutate")["mutation"]["properties"]
    assert "page_start" not in props
    assert "page_end" not in props


@pytest.mark.parametrize(("failures", "succeeds"), [(2, True), (3, False)])
def test_puts_retry_the_opaque_execution_failed_of_concurrent_first_writes(
    scope: OrgScope, failures: int, succeeds: bool
) -> None:
    """Observed on 3.0.10: racing first writes of one id sometimes fail; the merge retry works."""
    fake = FakeRushDb()
    remaining = [failures]

    def handler(request: httpx.Request) -> httpx.Response:
        op = json.loads(request.content)["operation"]
        if op["kind"] == "mutate" and remaining[0] > 0:
            remaining[0] -= 1
            fake.requests.append(request)
            return _err(422, "execution_failed", message="internal execution error")
        return fake.handler(request)

    settings = RushDbSettings(
        RUSHDB_TOKEN=SecretStr(TOKEN), RUSHDB_RETRY_BACKOFF_SECONDS=0, RUSHDB_MAX_ATTEMPTS=3
    )
    repo = RushDbKnowledgeRepository(
        settings,
        client=httpx.Client(base_url="http://x:1", transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
    )
    _d, _r, chunks, vectors = make_revision(scope, uuid.uuid4(), ["a b"], [[1.0, 0, 0, 0]])
    if succeeds:
        assert repo.upsert_chunks(scope, chunks, vectors) == 1
    else:
        with pytest.raises(KnowledgeStoreError) as info:
            repo.upsert_chunks(scope, chunks, vectors)
        assert info.value.code == "execution_failed"
        assert not isinstance(info.value, KnowledgeStoreUnavailable)


def test_dimensions_are_cached_per_tenant_and_space(fake: FakeRushDb, scope: OrgScope) -> None:
    repo, _ = make_repo(fake)
    _d, _r, chunks, vectors = make_revision(scope, uuid.uuid4(), ["a b"], [[1.0, 0, 0, 0]])
    repo.upsert_chunks(scope, chunks, vectors)
    repo.upsert_chunks(scope, chunks, vectors)
    describes = [
        o
        for o in fake.operations
        if o["kind"] == "describe" and o["describe"]["kind"] == "vectorIndex"
    ]
    assert len(describes) == 1
    other = fake_scope()
    _d, _r, c2, v2 = make_revision(other, uuid.uuid4(), ["a b"], [[1.0, 0, 0, 0]])
    repo.upsert_chunks(other, c2, v2)
    describes = [
        o
        for o in fake.operations
        if o["kind"] == "describe" and o["describe"]["kind"] == "vectorIndex"
    ]
    assert len(describes) == 2


def test_wrong_dimensions_for_the_remote_index_are_rejected(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    fake.dims = 768
    repo, _ = make_repo(fake)
    _d, _r, chunks, vectors = make_revision(scope, uuid.uuid4(), ["a b"], [[1.0, 0, 0, 0]])
    with pytest.raises(ValueError, match="768"):
        repo.upsert_chunks(scope, chunks, vectors)


# ------------------------------------------------------------------------- set_published


def test_set_published_flips_until_nothing_remains(scope: OrgScope) -> None:
    pending = [str(uuid.uuid4()) for _ in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        op = json.loads(request.content)["operation"]
        if op["kind"] == "mutate":
            pending.remove(op["mutation"]["id"])
            assert op["mutation"]["properties"] == {"published": True}
            return httpx.Response(200, json={"response": {"kind": "record", "record": {}}})
        query = op["query"]["query"]
        assert query["predicate"]["published"] is False
        if "$count" in json.dumps(query["select"]):
            return httpx.Response(
                200, json={"response": {"kind": "rows", "rows": [{"n": len(pending)}]}}
            )
        return httpx.Response(
            200,
            json={
                "response": {
                    "kind": "rows",
                    "rows": [{"chunk_id": c} for c in pending[: query["limit"]]],
                }
            },
        )

    settings = RushDbSettings(RUSHDB_TOKEN=SecretStr(TOKEN), RUSHDB_BATCH_SIZE=2)
    repo = RushDbKnowledgeRepository(
        settings, client=httpx.Client(base_url="http://x:1", transport=httpx.MockTransport(handler))
    )
    assert repo.set_published(scope, uuid.uuid4(), True) == 5
    assert pending == []
    assert repo.set_published(scope, uuid.uuid4(), True) == 0
    with pytest.raises(TypeError):
        repo.set_published(scope, uuid.uuid4(), "yes")  # type: ignore[arg-type]


def test_set_published_fails_loudly_if_the_store_ignores_the_flip(scope: OrgScope) -> None:
    stuck = [str(uuid.uuid4())]

    def handler(request: httpx.Request) -> httpx.Response:
        op = json.loads(request.content)["operation"]
        if op["kind"] == "mutate":
            return httpx.Response(200, json={"response": {"kind": "record", "record": {}}})
        if "$count" in json.dumps(op["query"]["query"]["select"]):
            return httpx.Response(200, json={"response": {"kind": "rows", "rows": [{"n": 1}]}})
        return httpx.Response(
            200, json={"response": {"kind": "rows", "rows": [{"chunk_id": stuck[0]}]}}
        )

    repo = RushDbKnowledgeRepository(
        RushDbSettings(RUSHDB_TOKEN=SecretStr(TOKEN)),
        client=httpx.Client(base_url="http://x:1", transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(KnowledgeStoreError, match="unflipped"):
        repo.set_published(scope, uuid.uuid4(), True)


# ------------------------------------------------------------------------------- purge


def test_deletes_remove_evidence_then_chunks_by_revision_or_document(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    repo, _ = make_repo(fake)
    rev, doc = uuid.uuid4(), uuid.uuid4()
    counts = repo.delete_revision(scope, rev)
    assert (counts.chunks, counts.evidence) == (2, 2)
    repo.delete_document(scope, doc)
    deletes = [o["mutation"]["query"] for o in fake.operations]
    assert [(d["labels"], d["predicate"]) for d in deletes] == [
        (["Evidence"], {"revision_id": str(rev)}),
        (["Chunk"], {"revision_id": str(rev)}),
        (["Evidence"], {"document_id": str(doc)}),
        (["Chunk"], {"document_id": str(doc)}),
    ]


def test_count_and_list_read_only_by_revision(fake: FakeRushDb, scope: OrgScope) -> None:
    repo, _ = make_repo(fake)
    rev = uuid.uuid4()
    fake.counts = [7]
    assert repo.count_chunks(scope, rev, published=True) == 7
    assert repo.list_chunk_ids(scope, rev) == []
    count_q, list_q = (o["query"]["query"] for o in fake.operations)
    assert count_q["predicate"] == {"revision_id": str(rev), "published": True}
    assert list_q["predicate"] == {"revision_id": str(rev)}


# ----------------------------------------------------------------------- error mapping


def _err(
    status: int, code: str, *, retryable: bool = False, message: str = "boom"
) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "protocolVersion": 1,
            "error": {"code": code, "message": message, "retryable": retryable},
        },
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("bad"),
        httpx.Response(500, text="oops"),
        httpx.Response(503, json={"error": {"code": "unavailable"}}),
        httpx.Response(401, json={"error": "unauthorized"}),
        _err(200, "execution_failed", retryable=True),
        _err(200, "unavailable"),
    ],
)
def test_transient_failures_map_to_unavailable_after_bounded_retries(
    fake: FakeRushDb, scope: OrgScope, failure: httpx.Response | Exception
) -> None:
    fake.fail_with = [failure] * 10
    repo, sleeps = make_repo(fake, attempts=3)
    with pytest.raises(KnowledgeStoreUnavailable):
        repo.count_chunks(scope, uuid.uuid4())
    assert len(fake.requests) == 3  # bounded: never more than 3 attempts
    assert len(sleeps) == 2  # short backoff between attempts only


def test_a_transient_failure_followed_by_success_is_retried(
    fake: FakeRushDb, scope: OrgScope
) -> None:
    fake.fail_with = [httpx.Response(502, text="bad gateway"), httpx.ConnectError("x")]
    repo, sleeps = make_repo(fake, attempts=3)
    assert repo.count_chunks(scope, uuid.uuid4()) == 0
    assert len(fake.requests) == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (_err(403, "missing_scope"), "missing_scope"),
        (_err(403, "scope_mismatch"), "scope_mismatch"),
        (_err(409, "conflict"), "conflict"),
        (_err(400, "invalid_request"), "invalid_request"),
        (_err(200, "execution_failed"), "execution_failed"),
        (httpx.Response(404, text="nope"), "http_404"),
    ],
)
def test_permanent_failures_map_to_store_error_without_retry(
    fake: FakeRushDb, scope: OrgScope, failure: httpx.Response, code: str
) -> None:
    fake.fail_with = [failure] * 5
    repo, sleeps = make_repo(fake)
    with pytest.raises(KnowledgeStoreError) as info:
        repo.count_chunks(scope, uuid.uuid4())
    assert not isinstance(info.value, KnowledgeStoreUnavailable)
    assert info.value.code == code
    assert len(fake.requests) == 1
    assert sleeps == []


def test_malformed_responses_are_store_errors(fake: FakeRushDb, scope: OrgScope) -> None:
    repo, _ = make_repo(fake)
    for response in (httpx.Response(200, text="<html>"), httpx.Response(200, json={"nope": 1}),
                     httpx.Response(200, json=[1, 2])):  # fmt: skip
        fake.fail_with = [response]
        with pytest.raises(KnowledgeStoreError, match="malformed"):
            repo.count_chunks(scope, uuid.uuid4())
    fake.fail_with = [
        httpx.Response(200, json={"response": {"kind": "rows", "rows": [{"n": "x"}]}})
    ]
    with pytest.raises(KnowledgeStoreError, match="malformed"):
        repo.count_chunks(scope, uuid.uuid4())
    fake.fail_with = [
        httpx.Response(
            200, json={"response": {"kind": "records", "records": [{"id": "x", "properties": {}}]}}
        )
    ]
    with pytest.raises(KnowledgeStoreError, match="malformed record"):
        repo.search_keyword(scope, "q", SearchFilters(frozenset({uuid.uuid4()})))


def test_missing_vector_index_is_a_clear_store_error(scope: OrgScope) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _err(404, "not_found")

    repo = RushDbKnowledgeRepository(
        RushDbSettings(RUSHDB_TOKEN=SecretStr(TOKEN)),
        client=httpx.Client(base_url="http://x:1", transport=httpx.MockTransport(handler)),
    )
    _d, _r, chunks, vectors = make_revision(scope, uuid.uuid4(), ["a b"], [[1.0, 0, 0, 0]])
    with pytest.raises(KnowledgeStoreError, match="ensure_indexes") as info:
        repo.upsert_chunks(scope, chunks, vectors)
    assert info.value.code == "index_missing"


# ------------------------------------------------------------------------------- secrets


def test_no_secrets_or_chunk_text_in_exceptions_or_logs(
    fake: FakeRushDb, scope: OrgScope, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="ukos.rushdb")  # create_app() may have raised "ukos"
    repo, _ = make_repo(fake)
    chunk = make_chunk(scope, uuid.uuid4(), text=f"{MARKER} body text")
    hostile_message = f"echo {MARKER} {TOKEN} http://user:pw@{HOST}/x"
    failures: list[httpx.Response | Exception] = [
        _err(400, "invalid_request", message=hostile_message),
        _err(200, "execution_failed", retryable=True, message=hostile_message),
        httpx.Response(500, text=hostile_message),
        httpx.ConnectError(hostile_message),
        httpx.ReadTimeout(hostile_message),
        httpx.Response(401, text=hostile_message),
    ]
    seen: list[str] = []
    for failure in failures:
        fake.fail_with = [failure] * 3
        with pytest.raises(KnowledgeStoreError) as info:
            repo.upsert_chunks(scope, [chunk], [[1.0, 0, 0, 0]])
        seen += [str(info.value), repr(info.value)]
        seen.append("".join(_cause_chain(info.value)))
    repo.count_chunks(scope, uuid.uuid4())  # a success is logged at DEBUG too
    # httpx's own INFO line names the (credential-free) origin; the adapter's lines must not.
    own_logs = "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")
    seen += [repr(repo), own_logs]
    blob = "\n".join(seen)
    for secret in (TOKEN, MARKER, "user:pw", HOST):
        assert secret not in blob, f"{secret!r} leaked"
    for secret in (TOKEN, MARKER, "user:pw"):  # nothing at all (httpx included) may carry these
        assert secret not in caplog.text, f"{secret!r} leaked into logs"
    assert "rushdb" in own_logs  # something was logged, so the check above is meaningful


def _cause_chain(exc: BaseException) -> list[str]:
    out: list[str] = []
    cur: BaseException | None = exc
    while cur is not None:
        out.append(f"{type(cur).__name__}:{cur}")
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
    return out


# ------------------------------------------------------------------------------ settings


def test_settings_defaults_and_secret_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUSHDB_TOKEN", TOKEN)
    monkeypatch.setenv("RUSHDB_URL", "http://rushdb:3000/")
    settings = RushDbSettings()
    assert settings.RUSHDB_URL == "http://rushdb:3000"
    assert settings.RUSHDB_TOKEN.get_secret_value() == TOKEN
    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings)
    assert (settings.RUSHDB_BATCH_SIZE, settings.RUSHDB_MAX_ATTEMPTS) == (100, 3)


def test_settings_validation_never_echoes_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUSHDB_TOKEN", raising=False)
    with pytest.raises(ValidationError) as missing:
        RushDbSettings()
    assert "RUSHDB_TOKEN" in str(missing.value)
    for bad_url in ("ftp://x", "http://u:pw@rushdb:3000", "http://rushdb:3000/path", "rushdb", ""):
        with pytest.raises(ValidationError) as info:
            RushDbSettings(RUSHDB_URL=bad_url, RUSHDB_TOKEN=SecretStr(TOKEN))
        assert "pw@" not in str(info.value)
        assert TOKEN not in str(info.value)
    for kwargs in (
        {"RUSHDB_BATCH_SIZE": 101},
        {"RUSHDB_BATCH_SIZE": 0},
        {"RUSHDB_MAX_ATTEMPTS": 4},
    ):
        with pytest.raises(ValidationError):
            RushDbSettings(RUSHDB_TOKEN=SecretStr(TOKEN), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RushDbSettings(RUSHDB_TOKEN=SecretStr(""))
