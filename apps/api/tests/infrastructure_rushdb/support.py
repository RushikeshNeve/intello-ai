"""Shared builders for the knowledge-repository tests (unit tests, contract suite, integration)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any, cast

from sqlalchemy.orm import Session

from app.modules.ingestion.ids import chunk_id_for
from app.modules.knowledge.ports import ChunkRecord, OrgScope, validated_org_scope

SPACE = "econtract0001"
DIMS = 4


class _Result:
    def __init__(self, row: tuple[int] | None) -> None:
        self._row = row

    def first(self) -> tuple[int] | None:
        return self._row


class FakeSession:
    """Just enough of a ``Session`` for ``validated_org_scope``: knows which orgs are live."""

    def __init__(self, live_orgs: Iterable[uuid.UUID] = ()) -> None:
        self.live = set(live_orgs)
        self.queries: list[str] = []

    def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.queries.append(str(statement))
        return _Result((1,) if params["id"] in self.live else None)


def fake_scope(org_id: uuid.UUID | None = None) -> OrgScope:
    """A real ``OrgScope`` minted through ``validated_org_scope`` against a fake live org."""
    org = org_id or uuid.uuid4()
    return validated_org_scope(cast(Session, FakeSession([org])), org)


def make_chunk(
    scope: OrgScope,
    workspace_id: uuid.UUID,
    *,
    text: str,
    revision_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    chunk_index: int = 0,
    space: str = SPACE,
    page: int | None = 1,
    heading: Sequence[str] = ("Intro",),
) -> ChunkRecord:
    revision = revision_id or uuid.uuid4()
    return ChunkRecord(
        org_id=scope.org_id,
        workspace_id=workspace_id,
        document_id=document_id or uuid.uuid4(),
        version_id=version_id or uuid.uuid4(),
        revision_id=revision,
        chunk_index=chunk_index,
        chunk_id=chunk_id_for(revision, chunk_index),
        text=text,
        token_estimate=max(1, len(text.split())),
        content_hash=f"sha256:{uuid.uuid5(revision, text).hex}",
        locators=[{"type": "page", "page": page}] if page is not None else [],
        heading_path=list(heading),
        page_start=page,
        page_end=page,
        embedding_space_id=space,
    )


def make_revision(
    scope: OrgScope,
    workspace_id: uuid.UUID,
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
    *,
    document_id: uuid.UUID | None = None,
    space: str = SPACE,
) -> tuple[uuid.UUID, uuid.UUID, list[ChunkRecord], list[Sequence[float]]]:
    """One revision of a fresh version: ``(document_id, revision_id, chunks, vectors)``."""
    document = document_id or uuid.uuid4()
    revision, version = uuid.uuid4(), uuid.uuid4()
    chunks = [
        make_chunk(
            scope,
            workspace_id,
            text=text,
            revision_id=revision,
            document_id=document,
            version_id=version,
            chunk_index=i,
            space=space,
        )
        for i, text in enumerate(texts)
    ]
    return document, revision, chunks, list(vectors)


ScopeFactory = Callable[[], OrgScope]


def assert_every_retrieval_is_scoped(
    operations: Iterable[dict[str, Any]], workspace_ids: set[str] | None = None
) -> int:
    """Fail unless every ranked/filtered retrieval carries ``workspace_id IN`` and ``published``.

    ``operations`` are the ``operation`` objects of a request log. ``workspace_ids`` (optional)
    pins the exact authorized set. Also fails on the forbidden query kinds (``text``, ``record``,
    ``statement``). Returns how many retrievals were checked.
    """
    checked = 0
    for op in operations:
        if op["kind"] != "query":
            continue
        kind = op["query"]["kind"]
        assert kind not in {"text", "record", "statement"}, f"forbidden query kind {kind}"
        if kind not in {"hybrid", "vector"}:
            continue
        inner = op["query"]["query"]
        predicate = inner["predicate"]
        authorized = set(predicate["workspace_id"]["$in"])  # KeyError = predicate missing
        assert authorized, "empty workspace predicate"
        if workspace_ids is not None:
            assert authorized == workspace_ids, "unexpected workspace predicate"
        assert predicate["published"] is True, "published predicate missing"
        assert inner["labels"] == ["Chunk"]
        checked += 1
    return checked
