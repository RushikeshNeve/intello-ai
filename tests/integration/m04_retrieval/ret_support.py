"""Helpers of the M04 retrieval integration tests: publishing documents (PostgreSQL rows, index
records and artifacts) the way the M03 pipeline leaves them, and a swappable index proxy."""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from catalog_support import Seeded, World, ensure_source, seed_document
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from tenancy_support import User

from app.core.db import tenant_session
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.modules.ai.embeddings import EmbeddingModelSpec, FakeEmbeddingClient
from app.modules.ingestion.artifacts import ArtifactStore, artifact_key, encode_chunks
from app.modules.ingestion.contracts import Chunk, Locator
from app.modules.ingestion.ids import PIPELINE_VERSION, chunk_id_for, revision_id_for
from app.modules.knowledge.ports import (
    ChunkRecord,
    OrgScope,
    SearchFilters,
    validated_org_scope,
)
from app.modules.retrieval.service import TokenBucketLimiter

SPEC = EmbeddingModelSpec(
    model_id="fake-embed-m04",
    dimensions=32,
    query_prefix="q: ",
    document_prefix="d: ",
    max_input_chars=6000,
    spec_version=1,
)


def purge_tenant(url: str, token: str, tenant: str) -> None:
    httpx.post(
        f"{url}/protocol/v1/execute",
        json={
            "protocolVersion": 1,
            "context": {"scope": tenant},
            "operation": {"kind": "purgeTenant"},
        },
        headers={"Authorization": f"Bearer {token}", "X-RushDB-Tenant": tenant},
        timeout=60.0,
    )


class RigHolder:
    """Mutable cell the app's dependency overrides read (the app is built once per session)."""

    current: Rig


class IndexProxy:
    """Delegates to the index; runs ``after`` hooks and can be told to fail (for race tests)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.after: dict[str, Callable[[], None]] = {}
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if name in self.fail:
                raise self.fail[name]
            result = target(*args, **kwargs)
            hook = self.after.get(name)
            if hook is not None:
                hook()
            return result

        return call


@dataclass(frozen=True)
class Published:
    revision_id: uuid.UUID
    chunk_ids: list[uuid.UUID]
    version_id: uuid.UUID
    document_id: uuid.UUID


class Rig:
    def __init__(self, admin: Engine, app: Engine, repo: Any, *, real: bool) -> None:
        self.admin = admin
        self.app = app
        self.repo = IndexProxy(repo)
        self.real = real
        self.embedder = FakeEmbeddingClient(SPEC)
        self.artifact_objects = InMemoryObjectStore()
        self.artifacts = ArtifactStore(self.artifact_objects)
        self.limiter = TokenBucketLimiter(100_000)
        self.orgs: set[uuid.UUID] = set()
        self._sources: dict[uuid.UUID, uuid.UUID] = {}

    # ------------------------------------------------------------------ sessions / scopes
    def org_scope(self, org_id: uuid.UUID) -> OrgScope:
        self.orgs.add(org_id)
        with Session(self.admin) as session:
            return validated_org_scope(session, org_id)

    @contextmanager
    def tenant(self, user: User, org_id: uuid.UUID) -> Iterator[Session]:
        """An app-role (RLS) session of ``user`` in ``org_id``."""
        with tenant_session(user_id=user.id, org_id=org_id, engine=self.app) as session:
            yield session

    def q(self, sql: str, **params: Any) -> list[Any]:
        with self.admin.connect() as conn:
            return list(conn.execute(text(sql), params).all())

    # ------------------------------------------------------------------------- seeding
    def seed(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        owner: User,
        *,
        title: str = "Doc",
        versions: int = 1,
        content_type: str = "text/plain",
        deleted: bool = False,
    ) -> Seeded:
        if workspace_id not in self._sources:
            self._sources[workspace_id] = ensure_source(self.admin, org_id, workspace_id, owner.id)
        return seed_document(
            self.admin,
            org_id=org_id,
            workspace_id=workspace_id,
            source_id=self._sources[workspace_id],
            created_by=owner.id,
            title=title,
            filename="file.txt",
            content_type=content_type,
            versions=versions,
            deleted=deleted,
        )

    def seed_world(self, world: World, **kw: Any) -> Seeded:
        return self.seed(world.org_id, world.ws, world.t.owner, **kw)

    def publish(
        self,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        doc: Seeded,
        texts: list[str],
        *,
        version: int = 1,
        index_texts: list[str] | None = None,
        flag: bool = True,
        point: bool = True,
        artifact: bool = True,
        wait: bool = True,
    ) -> Published:
        """Index ``texts`` as the revision of version ``version`` (1-based) of ``doc``.

        ``index_texts`` differ from the artifact text when a test needs the index to be stale;
        ``flag``/``point`` control the RushDB ``published`` flag and the PostgreSQL pointer.
        """
        version_id = doc.versions[version - 1]
        space = SPEC.space_id
        revision = revision_id_for(version_id, PIPELINE_VERSION, space)
        scope = self.org_scope(org_id)
        indexed = index_texts or texts
        chunk_ids = [chunk_id_for(revision, i) for i in range(len(texts))]
        records: list[ChunkRecord] = []
        chunks: list[Chunk] = []
        offset = 0
        for i, (body, idx_body) in enumerate(zip(texts, indexed, strict=True)):
            locator = Locator(
                kind="line",
                char_start=offset,
                char_end=offset + len(body),
                line_start=i + 1,
                line_end=i + 1,
            )
            chunks.append(
                Chunk(
                    chunk_index=i,
                    chunk_id=chunk_ids[i],
                    text=body,
                    token_estimate=math.ceil(len(body) / 4),
                    content_hash=hashlib.sha256(body.encode()).hexdigest(),
                    locators=[locator],
                    heading_path=["Heading"],
                )
            )
            records.append(
                ChunkRecord(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    document_id=doc.id,
                    version_id=version_id,
                    revision_id=revision,
                    chunk_index=i,
                    chunk_id=chunk_ids[i],
                    text=idx_body,
                    token_estimate=max(1, len(idx_body) // 4),
                    content_hash=hashlib.sha256(idx_body.encode()).hexdigest(),
                    locators=[
                        {"kind": "line", "char_start": offset, "char_end": offset + len(body)}
                    ],
                    heading_path=["Heading"],
                    page_start=None,
                    page_end=None,
                    embedding_space_id=space,
                )
            )
            offset += len(body) + 2
        self.repo.ensure_indexes(scope, space, SPEC.dimensions)
        self.repo.upsert_chunks(scope, records, self.embedder.embed_documents(indexed))

        payload = encode_chunks(chunks)
        key = artifact_key(org_id, revision, "chunk")
        if artifact:
            self.artifacts.put(key, payload, content_type="application/json")
        number = doc.versions.index(version_id) + 1
        with self.admin.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO knowledge_revisions (id, org_id, workspace_id, document_id,"
                    " version_id, pipeline_version, embedding_space_id, state, chunk_count,"
                    " ready_at, published_at) VALUES (:r, :o, :w, :d, :v, :pv, :sp, 'published',"
                    " :n, now(), now())"
                ),
                {
                    "r": revision,
                    "o": org_id,
                    "w": workspace_id,
                    "d": doc.id,
                    "v": version_id,
                    "pv": PIPELINE_VERSION,
                    "sp": space,
                    "n": len(texts),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, state,"
                    " input_fingerprint, artifact_key, artifact_sha256, artifact_bytes, attempts,"
                    " completed_at) VALUES (:r, 'chunk', :o, 'completed', :f, :k, :sha, :b, 1,"
                    " now())"
                ),
                {
                    "r": revision,
                    "o": org_id,
                    "f": "f" * 64,
                    "k": key,
                    "sha": hashlib.sha256(payload).hexdigest(),
                    "b": len(payload),
                },
            )
            if point:
                previous = conn.execute(
                    text("SELECT revision_id FROM document_publications WHERE document_id = :d"),
                    {"d": doc.id},
                ).scalar_one_or_none()
                if previous is None:
                    conn.execute(
                        text(
                            "INSERT INTO document_publications (document_id, org_id, workspace_id,"
                            " revision_id, version_id, version_number, published_at) VALUES"
                            " (:d, :o, :w, :r, :v, :n, now())"
                        ),
                        {
                            "d": doc.id,
                            "o": org_id,
                            "w": workspace_id,
                            "r": revision,
                            "v": version_id,
                            "n": number,
                        },
                    )
                else:
                    conn.execute(
                        text(
                            "UPDATE document_publications SET revision_id = :r, version_id = :v,"
                            " version_number = :n, previous_revision_id = :p, published_at = now()"
                            " WHERE document_id = :d"
                        ),
                        {"d": doc.id, "r": revision, "v": version_id, "n": number, "p": previous},
                    )
                    conn.execute(
                        text(
                            "UPDATE knowledge_revisions SET state = 'superseded',"
                            " superseded_at = now() WHERE id = :p"
                        ),
                        {"p": previous},
                    )
        if flag:
            self.repo.set_published(scope, revision, True)
            if wait and self.real:
                self.wait_searchable(scope, workspace_id, indexed[0])
        return Published(revision, chunk_ids, version_id, doc.id)

    def unflag(self, org_id: uuid.UUID, published: Published) -> None:
        self.repo.set_published(self.org_scope(org_id), published.revision_id, False)

    def wait_searchable(self, scope: OrgScope, workspace_id: uuid.UUID, body: str) -> None:
        """RushDB indexes asynchronously: wait until the chunk answers a keyword search."""
        filters = SearchFilters(frozenset({workspace_id}))
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if self.repo.search_keyword(scope, body, filters, limit=20):
                return
            time.sleep(0.2)
        raise AssertionError("chunk never became searchable")


def search(world: World, user: User | None, **body: Any) -> Any:
    return world.api.post(f"/v1/orgs/{world.org_id}/search", user, body)


def doc_ids(res: Any) -> list[str]:
    assert res.status == 200, res.body
    return [r["document_id"] for r in res.body["results"]]


def chunk_path(
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    chunk_id: uuid.UUID,
) -> str:
    return (
        f"/v1/orgs/{org_id}/workspaces/{workspace_id}/documents/{document_id}"
        f"/versions/{version_id}/chunks/{chunk_id}"
    )
