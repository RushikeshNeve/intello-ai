"""RushDB 3 knowledge repository (M03.03.01): the ONLY code that talks to RushDB.

Raw httpx over Protocol v1 (``POST /protocol/v1/execute``), exactly the wire calls verified in
``docs/spikes/rushdb-validation.md`` section 3 and ``tests/spikes/rushdb_*.py``.
Tenant = organization:
the scope ``org:<org_id>`` (from :class:`OrgScope`) is sent as BOTH the ``X-RushDB-Tenant`` header
and ``context.scope``.

Security design (spike section 7, DC-009/DC-023):

* Every search goes through :meth:`RushDbKnowledgeRepository._search`, which is the only place that
  builds a predicate (:func:`_scoped_predicate`): ``workspace_id IN (...) AND published = true``. No
  public method accepts a predicate or a statement, and ``query.text``, ``query.record`` and
  ``query.statement`` are never used (keyword search is a hybrid query with a text-only leg, because
  ``query.text`` cannot be filtered).
* Default write ``options: {}`` (never ``suggestTypes: false``), explicit ids and ``merge`` so
  retries and duplicate tasks never duplicate a record.
* Errors never include the token, the URL, the request body or chunk text; only the operation name
  and RushDB's short error code. Nothing here logs bodies.
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final

import httpx

from app.infrastructure.rushdb.settings import RushDbSettings
from app.modules.ai import egress
from app.modules.knowledge.ports import (
    MAX_UPSERT_BATCH,
    TEXT_INDEX_NAME,
    ChunkRecord,
    DeleteCounts,
    KnowledgeStoreError,
    KnowledgeStoreUnavailable,
    OrgScope,
    SearchFilters,
    SearchHit,
    validate_limit,
    validate_query_text,
    validate_space_id,
    validate_vector,
    vector_index_name,
)

_log = logging.getLogger("ukos.rushdb")

CHUNK_LABEL: Final = "Chunk"
EVIDENCE_LABEL: Final = "Evidence"
CLAIM_LABEL: Final = "Claim"
VECTOR_PROPERTY: Final = "embedding"
RRF_K: Final = 60
_PAGE: Final = 500  # list_chunk_ids page size (RushDB commits deletes in pages of 500 as well)

_PUT_RETRY_CODES: Final = frozenset({"execution_failed"})

_Json = dict[str, Any]


# ------------------------------------------------------------------------------- predicate


def _scoped_predicate(filters: SearchFilters, *, embedding_space_id: str | None = None) -> _Json:
    """THE predicate of every search: authorized workspaces AND published chunks.

    This is the only function that builds a read predicate. ``embedding_space_id`` (vector/hybrid
    only) additionally restricts to chunks of the space whose index is queried. The root label
    carries its own predicate because a filter on a related block constrains only that block.
    """
    predicate: _Json = {
        "workspace_id": {"$in": sorted(str(w) for w in filters.workspace_ids)},
        "published": True,
    }
    if embedding_space_id is not None:
        predicate["embedding_space_id"] = embedding_space_id
    return predicate


# --------------------------------------------------------------------------- repository


class RushDbKnowledgeRepository:
    """Sync httpx implementation of :class:`app.modules.knowledge.ports.KnowledgeRepository`."""

    def __init__(
        self,
        settings: RushDbSettings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=settings.RUSHDB_URL,
            event_hooks={"request": [egress.request_hook("rushdb")]},
            timeout=httpx.Timeout(
                settings.RUSHDB_READ_TIMEOUT_SECONDS,
                connect=settings.RUSHDB_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        self._sleep = sleep
        # (tenant, space) -> dimensions, filled by ensure_indexes or by describe on first use.
        self._dimensions: dict[tuple[str, str], int] = {}

    def __repr__(self) -> str:  # never leaks the token or the URL
        return "RushDbKnowledgeRepository()"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RushDbKnowledgeRepository:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------------ transport

    def _execute(
        self,
        scope: OrgScope,
        operation: _Json,
        *,
        what: str,
        retry_codes: frozenset[str] = frozenset(),
    ) -> _Json:
        """POST one operation; bounded retries on transient failures (all calls are idempotent).

        ``retry_codes`` names RushDB error codes that are also worth retrying for this call:
        concurrent first writes of the same record id intermittently fail with the opaque
        ``execution_failed`` (observed on 3.0.10), and the retry of a ``merge`` succeeds.
        """
        attempts = self._settings.RUSHDB_MAX_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                return self._execute_once(scope, operation, what=what)
            except KnowledgeStoreError as exc:
                retryable = isinstance(exc, KnowledgeStoreUnavailable) or exc.code in retry_codes
                if not retryable or attempt == attempts:
                    raise
                self._sleep(self._settings.RUSHDB_RETRY_BACKOFF_SECONDS * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _execute_once(self, scope: OrgScope, operation: _Json, *, what: str) -> _Json:
        tenant = scope.tenant
        envelope = {"protocolVersion": 1, "context": {"scope": tenant}, "operation": operation}
        started = time.perf_counter()
        try:
            response = self._client.post(
                "/protocol/v1/execute",
                json=envelope,
                headers={
                    "Authorization": f"Bearer {self._settings.RUSHDB_TOKEN.get_secret_value()}",
                    "X-RushDB-Tenant": tenant,
                },
            )
        except httpx.TimeoutException:
            raise KnowledgeStoreUnavailable(f"rushdb {what}: timeout") from None
        except httpx.HTTPError as exc:
            raise KnowledgeStoreUnavailable(
                f"rushdb {what}: connection failed ({type(exc).__name__})"
            ) from None
        _log.debug(
            "rushdb %s status=%s ms=%.1f",
            what,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return self._unwrap(response, what)

    @staticmethod
    def _unwrap(response: httpx.Response, what: str) -> _Json:
        status = response.status_code
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if status >= 500:
            raise KnowledgeStoreUnavailable(f"rushdb {what}: http {status}")
        if status == 401:
            raise KnowledgeStoreUnavailable(f"rushdb {what}: credentials rejected")
        if error is not None or status != 200:
            code = error.get("code") if isinstance(error, dict) else None
            code = code if isinstance(code, str) else None
            retryable = isinstance(error, dict) and error.get("retryable") is True
            if retryable or code == "unavailable":
                raise KnowledgeStoreUnavailable(
                    f"rushdb {what}: {code or 'unavailable'}", code=code
                )
            raise KnowledgeStoreError(
                f"rushdb {what}: {code or f'http {status}'}",
                code=code or f"http_{status}",
            )
        result = body.get("response") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise KnowledgeStoreError(f"rushdb {what}: malformed response", code="malformed")
        return result

    # ------------------------------------------------------------------------- indexes

    def ensure_indexes(self, scope: OrgScope, embedding_space_id: str, dimensions: int) -> None:
        space = validate_space_id(embedding_space_id)
        if isinstance(dimensions, bool) or not 1 <= dimensions <= 16384:
            raise ValueError("dimensions must be between 1 and 16384")
        self._execute(
            scope,
            {
                "kind": "index",
                "index": {
                    "kind": "createVector",
                    "definition": {
                        "name": vector_index_name(space),
                        "label": CHUNK_LABEL,
                        "property": VECTOR_PROPERTY,
                        "dimensions": dimensions,
                        "similarity": "cosine",
                        "model": space,
                    },
                },
            },
            what="ensure_indexes.vector",
        )
        # An existing index of other dimensions is a conflict from the server (immutable index).
        self._dimensions[(scope.tenant, space)] = dimensions
        if self._text_index_state(scope) is None:
            try:
                self._execute(
                    scope,
                    {
                        "kind": "index",
                        "index": {
                            "kind": "create",
                            "definition": {
                                "name": TEXT_INDEX_NAME,
                                "label": CHUNK_LABEL,
                                "property": "text",
                                "kind": "text",
                                "consistency": "immediate",
                            },
                        },
                    },
                    what="ensure_indexes.text",
                )
            except KnowledgeStoreError as exc:
                if exc.code != "conflict" or isinstance(exc, KnowledgeStoreUnavailable):
                    raise  # "conflict" = a concurrent caller created it first: fine
        deadline = time.monotonic() + self._settings.RUSHDB_INDEX_READY_TIMEOUT_SECONDS
        while self._text_index_state(scope) != "ready":
            if time.monotonic() > deadline:
                raise KnowledgeStoreUnavailable("rushdb ensure_indexes: text index not ready")
            self._sleep(0.1)

    def _text_index_state(self, scope: OrgScope) -> str | None:
        described = self._execute(
            scope, {"kind": "describe", "describe": {"kind": "indexes"}}, what="describe.indexes"
        )
        for index in described.get("indexes", []):
            if isinstance(index, dict) and index.get("name") == TEXT_INDEX_NAME:
                state = index.get("state")
                return state if isinstance(state, str) else "unknown"
        return None

    def _index_dimensions(self, scope: OrgScope, space: str) -> int:
        cached = self._dimensions.get((scope.tenant, space))
        if cached is not None:
            return cached
        try:
            described = self._execute(
                scope,
                {
                    "kind": "describe",
                    "describe": {"kind": "vectorIndex", "name": vector_index_name(space)},
                },
                what="describe.vectorIndex",
            )
            dims = described["stats"]["index"]["dimensions"]
        except KnowledgeStoreUnavailable:
            raise
        except (KnowledgeStoreError, KeyError, TypeError):
            raise KnowledgeStoreError(
                "rushdb: vector index for the embedding space is missing; call ensure_indexes",
                code="index_missing",
            ) from None
        if not isinstance(dims, int):
            raise KnowledgeStoreError("rushdb: malformed index description", code="malformed")
        self._dimensions[(scope.tenant, space)] = dims
        return dims

    # -------------------------------------------------------------------------- writes

    def upsert_chunks(
        self,
        scope: OrgScope,
        chunks: Sequence[ChunkRecord],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        if not chunks:
            return 0
        space = chunks[0].embedding_space_id
        for chunk in chunks:
            if chunk.org_id != scope.org_id:
                raise ValueError("chunk belongs to a different organization than the scope")
            if chunk.embedding_space_id != space:
                raise ValueError("all chunks of one call must share one embedding space")
        dimensions = self._index_dimensions(scope, space)
        for vector in vectors:
            validate_vector(vector, dimensions)  # whole input checked before the first write
        index = vector_index_name(space)
        batch = min(self._settings.RUSHDB_BATCH_SIZE, MAX_UPSERT_BATCH)
        written = 0
        for start in range(0, len(chunks), batch):
            part = chunks[start : start + batch]
            published = self._published_ids(scope, [c.chunk_id for c in part])
            for chunk, vector in zip(part, vectors[start : start + batch], strict=True):
                self._put_chunk(
                    scope, chunk, vector, index, keep_published=chunk.chunk_id in published
                )
                written += 1
        return written

    def _published_ids(self, scope: OrgScope, chunk_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these chunks already exist AND are published (a re-write must not hide them)."""
        result = self._execute(
            scope,
            {
                "kind": "query",
                "query": {
                    "kind": "records",
                    "query": {
                        "labels": [CHUNK_LABEL],
                        "predicate": {
                            "$id": {"$in": [str(c) for c in chunk_ids]},
                            "published": True,
                        },
                        "select": {"chunk_id": "chunk_id"},
                        "limit": len(chunk_ids),
                    },
                },
            },
            what="upsert_chunks.lookup",
        )
        return {
            uuid.UUID(str(row["chunk_id"])) for row in result.get("rows", []) if "chunk_id" in row
        }

    def _put_chunk(
        self,
        scope: OrgScope,
        chunk: ChunkRecord,
        vector: Sequence[float],
        index: str,
        *,
        keep_published: bool,
    ) -> None:
        properties: _Json = {
            "chunk_id": str(chunk.chunk_id),
            "org_id": str(chunk.org_id),
            "workspace_id": str(chunk.workspace_id),
            "document_id": str(chunk.document_id),
            "version_id": str(chunk.version_id),
            "revision_id": str(chunk.revision_id),
            "chunk_index": chunk.chunk_index,
            "text": chunk.text,
            "token_estimate": chunk.token_estimate,
            "content_hash": chunk.content_hash,
            "locators": json.dumps(list(chunk.locators), separators=(",", ":")),
            "heading_path": json.dumps(list(chunk.heading_path), separators=(",", ":")),
            "embedding_space_id": chunk.embedding_space_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if chunk.page_start is not None and chunk.page_end is not None:
            properties["page_start"] = chunk.page_start
            properties["page_end"] = chunk.page_end
        if not keep_published:
            properties["published"] = False
        self._execute(
            scope,
            {
                "kind": "mutate",
                "mutation": {
                    "kind": "putRecord",
                    "mode": "merge",
                    "label": CHUNK_LABEL,
                    "id": str(chunk.chunk_id),
                    "properties": properties,
                    "options": {},
                    "vectors": [
                        {
                            "propertyName": VECTOR_PROPERTY,
                            "vector": [float(v) for v in vector],
                            "index": index,
                        }
                    ],
                },
            },
            what="upsert_chunks.put",
            retry_codes=_PUT_RETRY_CODES,
        )

    def set_published(self, scope: OrgScope, revision_id: uuid.UUID, flag: bool) -> int:
        """Flip ``published`` on every chunk (and graph claim) of the revision; returns chunks."""
        if not isinstance(flag, bool):
            raise TypeError("flag must be a bool")
        changed = self._set_published_label(scope, CHUNK_LABEL, "chunk_id", revision_id, flag)
        self._set_published_label(scope, CLAIM_LABEL, "claim_id", revision_id, flag)
        return changed

    def _set_published_label(
        self, scope: OrgScope, label: str, id_prop: str, revision_id: uuid.UUID, flag: bool
    ) -> int:
        remaining = self._count(
            scope, {"revision_id": str(revision_id), "published": not flag}, label=label
        )
        changed = 0
        # Each pass takes the first page of the not-yet-flipped records; it shrinks as we flip.
        for _ in range(remaining // self._settings.RUSHDB_BATCH_SIZE + 2):
            page = self._execute(
                scope,
                {
                    "kind": "query",
                    "query": {
                        "kind": "records",
                        "query": {
                            "labels": [label],
                            "predicate": {"revision_id": str(revision_id), "published": not flag},
                            "select": {id_prop: id_prop},
                            "limit": self._settings.RUSHDB_BATCH_SIZE,
                        },
                    },
                },
                what="set_published.list",
            )
            ids = [str(r[id_prop]) for r in page.get("rows", []) if id_prop in r]
            if not ids:
                return changed
            for record_id in ids:
                self._execute(
                    scope,
                    {
                        "kind": "mutate",
                        "mutation": {
                            "kind": "putRecord",
                            "mode": "merge",
                            "label": label,
                            "id": record_id,
                            "properties": {"published": flag},
                            "options": {},
                        },
                    },
                    what="set_published.put",
                    retry_codes=_PUT_RETRY_CODES,
                )
                changed += 1
        if (
            self._count(
                scope, {"revision_id": str(revision_id), "published": not flag}, label=label
            )
            > 0
        ):
            raise KnowledgeStoreError(
                "rushdb set_published: records remain unflipped", code="incomplete"
            )
        return changed

    def delete_revision(self, scope: OrgScope, revision_id: uuid.UUID) -> DeleteCounts:
        return self._purge(scope, {"revision_id": str(revision_id)})

    def delete_document(self, scope: OrgScope, document_id: uuid.UUID) -> DeleteCounts:
        return self._purge(scope, {"document_id": str(document_id)})

    def _purge(self, scope: OrgScope, predicate: _Json) -> DeleteCounts:
        """Spike section 3.9 steps 1-3: Evidence, then Claims (M05), then Chunks.

        Entities carry no revision/document id and are not swept here (they are only ever reachable
        through live claims). ``deleteRecords`` is paged and not one transaction, so PostgreSQL
        denial must come
        first; every step is idempotent and a second call returns zero counts.
        """
        counts: dict[str, int] = {}
        for label in (EVIDENCE_LABEL, CLAIM_LABEL, CHUNK_LABEL):
            result = self._execute(
                scope,
                {
                    "kind": "mutate",
                    "mutation": {
                        "kind": "deleteRecords",
                        "query": {"labels": [label], "predicate": predicate},
                    },
                },
                what=f"delete.{label.lower()}",
            )
            affected = result.get("affected", 0)
            counts[label] = affected if isinstance(affected, int) else 0
        return DeleteCounts(
            chunks=counts[CHUNK_LABEL], evidence=counts[EVIDENCE_LABEL], claims=counts[CLAIM_LABEL]
        )

    # --------------------------------------------------------------------------- reads

    def count_chunks(
        self, scope: OrgScope, revision_id: uuid.UUID, published: bool | None = None
    ) -> int:
        predicate: _Json = {"revision_id": str(revision_id)}
        if published is not None:
            predicate["published"] = published
        return self._count(scope, predicate)

    def _count(self, scope: OrgScope, predicate: _Json, *, label: str = CHUNK_LABEL) -> int:
        result = self._execute(
            scope,
            {
                "kind": "query",
                "query": {
                    "kind": "records",
                    "query": {
                        "labels": [label],
                        "predicate": predicate,
                        "select": {"n": {"$count": "*"}},
                    },
                },
            },
            what="count_chunks",
        )
        rows = result.get("rows", [])
        if not rows:
            return 0
        n = rows[0].get("n") if isinstance(rows[0], dict) else None
        if not isinstance(n, int):
            raise KnowledgeStoreError("rushdb count_chunks: malformed response", code="malformed")
        return n

    def list_chunk_ids(self, scope: OrgScope, revision_id: uuid.UUID) -> list[uuid.UUID]:
        ids: list[uuid.UUID] = []
        offset = 0
        while True:
            page = self._execute(
                scope,
                {
                    "kind": "query",
                    "query": {
                        "kind": "records",
                        "query": {
                            "labels": [CHUNK_LABEL],
                            "predicate": {"revision_id": str(revision_id)},
                            "select": {"chunk_id": "chunk_id", "chunk_index": "chunk_index"},
                            "order": {"chunk_index": "asc"},
                            "offset": offset,
                            "limit": _PAGE,
                        },
                    },
                },
                what="list_chunk_ids",
            )
            rows = [r for r in page.get("rows", []) if isinstance(r, dict) and "chunk_id" in r]
            ids.extend(uuid.UUID(str(r["chunk_id"])) for r in rows)
            if len(rows) < _PAGE:
                return ids
            offset += _PAGE

    # ------------------------------------------------------------------------- searches

    def search_keyword(
        self, scope: OrgScope, query: str, filters: SearchFilters, *, limit: int = 10
    ) -> list[SearchHit]:
        return self._search(
            scope,
            filters,
            limit=limit,
            what="search_keyword",
            text=validate_query_text(query),
            vector=None,
            space=None,
        )

    def search_vector(
        self,
        scope: OrgScope,
        embedding_space_id: str,
        vector: Sequence[float],
        filters: SearchFilters,
        *,
        limit: int = 10,
    ) -> list[SearchHit]:
        space = validate_space_id(embedding_space_id)
        validate_vector(vector, self._index_dimensions(scope, space))
        return self._search(
            scope, filters, limit=limit, what="search_vector", text=None, vector=vector, space=space
        )

    def search_hybrid(
        self,
        scope: OrgScope,
        embedding_space_id: str,
        query: str,
        vector: Sequence[float],
        filters: SearchFilters,
        *,
        limit: int = 10,
    ) -> list[SearchHit]:
        space = validate_space_id(embedding_space_id)
        validate_vector(vector, self._index_dimensions(scope, space))
        return self._search(
            scope,
            filters,
            limit=limit,
            what="search_hybrid",
            text=validate_query_text(query),
            vector=vector,
            space=space,
        )

    def _search(
        self,
        scope: OrgScope,
        filters: SearchFilters,
        *,
        limit: int,
        what: str,
        text: str | None,
        vector: Sequence[float] | None,
        space: str | None,
    ) -> list[SearchHit]:
        """The single choke point of every retrieval read (spike section 7 rules 2 and 3)."""
        if not isinstance(filters, SearchFilters):
            raise TypeError("filters must be SearchFilters")
        validate_limit(limit)
        predicate = _scoped_predicate(filters, embedding_space_id=space)
        if text is not None and vector is None:
            # BM25 with filter-before-rank = hybrid with only a text leg (query.text can't filter).
            operation = _hybrid_operation(predicate, limit, text=text, vector=None, space=None)
        elif text is None and vector is not None and space is not None:
            operation = {
                "kind": "query",
                "query": {
                    "kind": "vector",
                    "query": {
                        "labels": [CHUNK_LABEL],
                        "property": VECTOR_PROPERTY,
                        "index": vector_index_name(space),
                        "vector": [float(v) for v in vector],
                        "limit": limit,
                        "mode": "auto",
                        "predicate": predicate,
                    },
                },
            }
        elif text is not None and vector is not None and space is not None:
            operation = _hybrid_operation(predicate, limit, text=text, vector=vector, space=space)
        else:  # pragma: no cover - private helper, callers above always pass a valid combination
            raise ValueError("a search needs a text leg, a vector leg or both")
        result = self._execute(scope, operation, what=what)
        hits: list[SearchHit] = []
        for record in result.get("records", []):
            hits.append(_hit_from_record(record, what))
        return hits


def _hybrid_operation(
    predicate: _Json,
    limit: int,
    *,
    text: str,
    vector: Sequence[float] | None,
    space: str | None,
) -> _Json:
    query: _Json = {
        "labels": [CHUNK_LABEL],
        "limit": limit,
        "predicate": predicate,
        "text": {"index": TEXT_INDEX_NAME, "query": text},
    }
    if vector is not None and space is not None:
        query["vector"] = {
            "property": VECTOR_PROPERTY,
            "vector": [float(v) for v in vector],
            "index": vector_index_name(space),
        }
        query["fusion"] = "rrf"
        query["rrfK"] = RRF_K
    return {"kind": "query", "query": {"kind": "hybrid", "query": query}}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _hit_from_record(record: Any, what: str) -> SearchHit:
    try:
        props: Mapping[str, Any] = record["properties"]
        score = float(record.get("score", 0.0))
        if not math.isfinite(score):
            score = 0.0
        return SearchHit(
            chunk_id=uuid.UUID(str(record["id"])),
            document_id=uuid.UUID(str(props["document_id"])),
            version_id=uuid.UUID(str(props["version_id"])),
            revision_id=uuid.UUID(str(props["revision_id"])),
            workspace_id=uuid.UUID(str(props["workspace_id"])),
            score=score,
            text=str(props.get("text", "")),
            locators=[x for x in _json_list(props.get("locators")) if isinstance(x, dict)],
            heading_path=[str(x) for x in _json_list(props.get("heading_path"))],
            page_start=_opt_int(props.get("page_start")),
            page_end=_opt_int(props.get("page_end")),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise KnowledgeStoreError(f"rushdb {what}: malformed record", code="malformed") from None
