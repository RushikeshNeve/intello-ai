"""Build the evaluation environment: raw files -> REAL extraction + chunking -> in-memory index.

Every document version goes through ``app.modules.ingestion`` (processors and ``chunk_document``),
is embedded with the deterministic ``FakeEmbeddingClient`` and written to
``InMemoryKnowledgeRepository`` exactly like the M03 pipeline would: the newest version of a live
document is published, older versions stay indexed but unpublished (superseded), and a deleted
document is purged from the index. A ``chunks`` registry (the evaluation's stand-in for the
PostgreSQL revalidation catalog) remembers every chunk ever indexed and its state.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from loader import DEFAULT_CORPUS, Manifest, load_manifest

from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.modules.ai.embeddings.fake import FakeEmbeddingClient
from app.modules.ingestion.chunking import chunk_document
from app.modules.ingestion.contracts import SourceInfo
from app.modules.ingestion.ids import PIPELINE_VERSION, revision_id_for
from app.modules.ingestion.processors.registry import extract
from app.modules.knowledge import ports
from app.modules.knowledge.ports import ChunkRecord, OrgScope

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://intello.ai/evaluation")
ChunkState = Literal["published", "superseded", "deleted"]


def eval_uuid(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(parts))


@dataclass(frozen=True)
class ChunkInfo:
    chunk_id: str
    document_id: str
    workspace_id: str
    version: int
    state: ChunkState
    text: str
    content_hash: str


@dataclass
class EvalEnv:
    manifest: Manifest
    repo: InMemoryKnowledgeRepository
    embedder: FakeEmbeddingClient
    space_id: str
    scopes: dict[str, OrgScope]  # org id -> scope
    org_uuid: dict[str, uuid.UUID]
    workspace_uuid: dict[str, uuid.UUID]
    org_of_workspace: dict[str, str]
    chunks: dict[str, ChunkInfo] = field(default_factory=dict)  # str(chunk uuid) -> info

    def chunk_count(self, state: ChunkState | None = None) -> int:
        return sum(1 for c in self.chunks.values() if state is None or c.state == state)


def build_environment(corpus_dir: Path = DEFAULT_CORPUS) -> EvalEnv:
    manifest = load_manifest(corpus_dir)
    embedder = FakeEmbeddingClient()
    space = embedder.spec.space_id
    repo = InMemoryKnowledgeRepository()
    org_uuid = {o: eval_uuid("org", o) for o in manifest.orgs}
    ws_uuid = {w: eval_uuid("workspace", w) for w in manifest.workspaces}
    scopes = {o: ports.OrgScope(u, _mint=ports._MINT) for o, u in org_uuid.items()}
    for scope in scopes.values():
        repo.ensure_indexes(scope, space, embedder.spec.dimensions)
    env = EvalEnv(
        manifest=manifest,
        repo=repo,
        embedder=embedder,
        space_id=space,
        scopes=scopes,
        org_uuid=org_uuid,
        workspace_uuid=ws_uuid,
        org_of_workspace=dict(manifest.workspaces),
    )
    for doc in manifest.documents:
        org = manifest.workspaces[doc.workspace]
        scope = scopes[org]
        document_uuid = eval_uuid("document", doc.id)
        latest = doc.current.version
        for spec in sorted(doc.versions, key=lambda v: v.version):
            data = manifest.read(spec)
            version_uuid = eval_uuid("version", doc.id, str(spec.version))
            extracted = extract(
                data,
                doc.content_type,
                source=SourceInfo(
                    version_id=version_uuid,
                    sha256=hashlib.sha256(data).hexdigest(),
                    content_type=doc.content_type,
                    byte_size=len(data),
                ),
            )
            revision = revision_id_for(version_uuid, PIPELINE_VERSION, space)
            chunks = chunk_document(extracted, revision)
            records = [
                ChunkRecord(
                    org_id=org_uuid[org],
                    workspace_id=ws_uuid[doc.workspace],
                    document_id=document_uuid,
                    version_id=version_uuid,
                    revision_id=revision,
                    chunk_index=c.chunk_index,
                    chunk_id=c.chunk_id,
                    text=c.text,
                    token_estimate=c.token_estimate,
                    content_hash=c.content_hash,
                    locators=[loc.model_dump(mode="json") for loc in c.locators],
                    heading_path=list(c.heading_path),
                    page_start=c.page_start,
                    page_end=c.page_end,
                    embedding_space_id=space,
                )
                for c in chunks
            ]
            vectors = embedder.embed_documents([r.text for r in records])
            repo.upsert_chunks(scope, records, vectors)
            is_latest = spec.version == latest
            state: ChunkState = "published" if is_latest and not doc.deleted else "superseded"
            if is_latest and not doc.deleted:
                repo.set_published(scope, revision, True)
            if doc.deleted:
                state = "deleted"
            for r in records:
                env.chunks[str(r.chunk_id)] = ChunkInfo(
                    chunk_id=str(r.chunk_id),
                    document_id=doc.id,
                    workspace_id=doc.workspace,
                    version=spec.version,
                    state=state,
                    text=r.text,
                    content_hash=r.content_hash,
                )
        if doc.deleted:
            repo.delete_document(scope, document_uuid)
    return env
