"""Builders for the retrieval unit tests: a fake session, scopes and indexed documents."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy.orm import Session

from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.modules.ai.embeddings import EmbeddingModelSpec, FakeEmbeddingClient
from app.modules.ingestion.ids import chunk_id_for
from app.modules.knowledge.ports import ChunkRecord, OrgScope, validated_org_scope
from app.modules.retrieval.scope import RetrievalScope, resolve_scope

SPEC = EmbeddingModelSpec(
    model_id="fake-embed",
    dimensions=64,
    query_prefix="q: ",
    document_prefix="d: ",
    max_input_chars=6000,
    spec_version=1,
)


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeDb:
    """Answers the few statements scope resolution and revalidation issue."""

    def __init__(
        self,
        *,
        org_id: uuid.UUID,
        role: str | None = "owner",
        workspaces: Iterable[uuid.UUID] = (),
        published: Iterable[Any] = (),
    ) -> None:
        self.org_id = org_id
        self.role = role
        self.workspaces = list(workspaces)
        self.published = list(published)
        self.statements: list[str] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "FROM organizations" in sql:
            return _Result([(1,)] if params and params["id"] == self.org_id else [])
        if "organization_memberships" in sql:
            return _Result([] if self.role is None else [self.role])
        if "document_publications" in sql:
            wanted = set(params["revs"]) if params else set()
            return _Result([p for p in self.published if p.revision_id in wanted])
        if "FROM workspaces" in sql:
            return _Result([(w,) for w in self.workspaces])
        raise AssertionError(f"unexpected statement: {sql}")

    @property
    def session(self) -> Session:
        return cast(Session, self)


def make_scope(
    org_id: uuid.UUID,
    workspace_ids: Iterable[uuid.UUID],
    *,
    user_id: uuid.UUID | None = None,
    requested: Iterable[uuid.UUID] | None = None,
) -> RetrievalScope:
    db = FakeDb(org_id=org_id, workspaces=workspace_ids)
    return resolve_scope(
        db.session,
        org_id=org_id,
        user_id=user_id or uuid.uuid4(),
        requested_workspace_ids=requested,
    )


def org_scope(org_id: uuid.UUID) -> OrgScope:
    return validated_org_scope(FakeDb(org_id=org_id).session, org_id)


class Indexed:
    def __init__(
        self,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        version_id: uuid.UUID,
        chunks: list[ChunkRecord],
    ) -> None:
        self.document_id = document_id
        self.revision_id = revision_id
        self.version_id = version_id
        self.chunks = chunks


def new_repo(
    org: OrgScope, embedder: FakeEmbeddingClient | None = None
) -> InMemoryKnowledgeRepository:
    repo = InMemoryKnowledgeRepository()
    spec = embedder.spec if embedder else SPEC
    repo.ensure_indexes(org, spec.space_id, spec.dimensions)
    return repo


def index_document(
    repo: InMemoryKnowledgeRepository,
    org: OrgScope,
    embedder: FakeEmbeddingClient,
    workspace_id: uuid.UUID,
    texts: Sequence[str],
    *,
    document_id: uuid.UUID | None = None,
    publish: bool = True,
) -> Indexed:
    document = document_id or uuid.uuid4()
    revision, version = uuid.uuid4(), uuid.uuid4()
    chunks: list[ChunkRecord] = []
    offset = 0
    for i, text in enumerate(texts):
        chunks.append(
            ChunkRecord(
                org_id=org.org_id,
                workspace_id=workspace_id,
                document_id=document,
                version_id=version,
                revision_id=revision,
                chunk_index=i,
                chunk_id=chunk_id_for(revision, i),
                text=text,
                token_estimate=max(1, len(text) // 4),
                content_hash=f"h{revision.hex}{i}",
                locators=[{"kind": "line", "char_start": offset, "char_end": offset + len(text)}],
                heading_path=["Section"],
                page_start=1,
                page_end=1,
                embedding_space_id=embedder.spec.space_id,
            )
        )
        offset += len(text) + 2
    vectors = embedder.embed_documents(list(texts))
    repo.upsert_chunks(org, chunks, vectors)
    if publish:
        repo.set_published(org, revision, True)
    return Indexed(document, revision, version, chunks)


def pub_row(
    ix: Indexed, workspace_id: uuid.UUID, *, title: str = "Doc", version_number: int = 1
) -> SimpleNamespace:
    return SimpleNamespace(
        revision_id=ix.revision_id,
        document_id=ix.document_id,
        version_id=ix.version_id,
        workspace_id=workspace_id,
        version_number=version_number,
        content_type="text/plain",
        title=title,
        workspace_slug="ws",
    )
