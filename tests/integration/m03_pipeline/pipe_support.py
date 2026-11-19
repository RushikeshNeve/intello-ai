"""Helpers of the M03 pipeline integration tests (a harness around real PostgreSQL)."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from m02_seed import insert_version
from m03_support import Scope, insert_job, seed_scope
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.db import tenant_session
from app.modules.ai.embeddings import EmbeddingModelSpec, FakeEmbeddingClient
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.ingestion.ids import PIPELINE_VERSION, revision_id_for
from app.modules.ingestion.pipeline import DefaultPipelineRunner
from app.modules.ingestion.runner_types import ClaimedJob, JobContext
from app.modules.knowledge.ports import OrgScope, SearchFilters, SearchHit, validated_org_scope
from app.workers.leases import job_is_ours

SPEC = EmbeddingModelSpec(
    model_id="fake-embed",
    dimensions=32,
    query_prefix="q: ",
    document_prefix="d: ",
    max_input_chars=6000,
    spec_version=1,
)
SPEC_B = EmbeddingModelSpec(
    model_id="fake-embed-b",
    dimensions=16,
    query_prefix="q: ",
    document_prefix="d: ",
    max_input_chars=6000,
    spec_version=1,
)


class SimulatedCrash(BaseException):
    """A process death: not an ``Exception``, so no handler in the pipeline may swallow it."""


class RepoProxy:
    """Delegates to a repository, counts calls and runs optional ``before`` hooks."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: Counter[str] = Counter()
        self.before: dict[str, Callable[..., None]] = {}

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            hook = self.before.get(name)
            if hook is not None:
                hook(*args, **kwargs)
            return target(*args, **kwargs)

        return call


def make_settings() -> Any:
    """The ``INGEST_*`` values the pipeline reads (small chunks: many chunks from small texts)."""
    return SimpleNamespace(
        INGEST_MAX_PDF_PAGES=2000,
        INGEST_MAX_EXTRACT_BYTES=50 * 1024 * 1024,
        INGEST_CHUNK_TARGET_CHARS=300,
        INGEST_CHUNK_OVERLAP_CHARS=50,
        INGEST_MAX_CHUNKS=20_000,
        INGEST_EMBED_BATCH_SIZE=4,
    )


class Env:
    def __init__(
        self,
        admin: Engine,
        worker: Engine,
        repo: Any,
        uploads: Any,
        artifacts_store: Any,
        *,
        rushdb: Any = None,
    ) -> None:
        self.admin = admin
        self.worker = worker
        self.repo = RepoProxy(repo)
        self.uploads = uploads
        self.artifacts_raw = artifacts_store
        self.artifacts = ArtifactStore(artifacts_store)
        self.embedder = FakeEmbeddingClient(SPEC)
        self.rushdb = rushdb
        self.orgs: list[uuid.UUID] = []
        self._numbers: Counter[uuid.UUID] = Counter()

    # ------------------------------------------------------------------- sessions and scopes

    @contextmanager
    def sessions(self, org_id: uuid.UUID) -> Iterator[Session]:
        with tenant_session(user_id=None, org_id=org_id, engine=self.worker) as session:
            yield session

    def scope_of(self, org_id: uuid.UUID) -> OrgScope:
        with Session(self.admin) as s:
            return validated_org_scope(s, org_id)

    def cleanup(self, purge: Callable[[str, str, str], None]) -> None:
        if self.rushdb is None:
            return
        for org in self.orgs:
            purge(self.rushdb.RUSHDB_URL, self.rushdb.RUSHDB_TOKEN.get_secret_value(), f"org:{org}")

    # --------------------------------------------------------------------------- seeding

    def new_scope(self) -> Scope:
        scope = seed_scope(self.admin)
        self.orgs.append(scope.org_id)
        self._numbers[scope.document_id] = 1  # the seeded version is number 1
        return scope

    def version(
        self,
        scope: Scope,
        data: bytes,
        *,
        content_type: str = "text/plain",
        stored: bytes | None = None,
        upload: bool = True,
    ) -> uuid.UUID:
        """A new version of the scope's document whose original is ``data`` in the uploads store
        (``stored`` lets a test store different bytes than the recorded checksum covers)."""
        self._numbers[scope.document_id] += 1
        number = self._numbers[scope.document_id]
        version_id = uuid.uuid4()
        key = f"org/{scope.org_id}/doc/{scope.document_id}/{version_id}/original"
        with self.admin.begin() as conn:
            insert_version(
                conn,
                org_id=scope.org_id,
                workspace_id=scope.workspace_id,
                document_id=scope.document_id,
                created_by=scope.owner_id,
                version_number=number,
                id=version_id,
                ct=content_type,
                sz=len(data),
                sha=hashlib.sha256(data).hexdigest(),
                k=key,
            )
        if upload:
            blob = data if stored is None else stored
            import base64

            self.uploads.put_object(
                key,
                blob,
                content_type="application/octet-stream",
                sha256_b64=base64.b64encode(hashlib.sha256(blob).digest()).decode(),
            )
        return version_id

    def job(
        self, scope: Scope, version_id: uuid.UUID, *, space: str | None = None, epoch: int = 1
    ) -> ClaimedJob:
        space_id = space or SPEC.space_id
        with self.admin.begin() as conn:
            job_id = insert_job(
                conn,
                scope,
                state="running",
                attempts=1,
                lease_owner="w1",
                lease_expires_in=600,
                lease_epoch=epoch,
                version_id=version_id,
                space=space_id,
            )
        return ClaimedJob(
            job_id=job_id,
            org_id=scope.org_id,
            workspace_id=scope.workspace_id,
            document_id=scope.document_id,
            version_id=version_id,
            revision_id=revision_id_for(version_id, PIPELINE_VERSION, space_id),
            attempts=1,
            lease_epoch=epoch,
            lease_owner="w1",
        )

    def ctx(self, claim: ClaimedJob) -> JobContext:
        """A real ``JobContext`` over the worker engine; ``check_fence`` checks the job row."""

        def check_fence() -> None:
            with self.sessions(claim.org_id) as s:
                job_is_ours(s, claim)

        def set_stage(stage: str) -> None:
            with self.sessions(claim.org_id) as s:
                job_is_ours(s, claim)
                s.execute(
                    text("UPDATE ingestion_jobs SET current_stage = :s WHERE id = :j"),
                    {"s": stage, "j": claim.job_id},
                )

        return JobContext(
            session_factory=self.sessions,
            heartbeat=lambda: None,
            check_fence=check_fence,
            set_stage=set_stage,
            settings=make_settings(),
        )

    def runner(self, **overrides: Any) -> DefaultPipelineRunner:
        kwargs: dict[str, Any] = {
            "uploads": self.uploads,
            "artifacts": self.artifacts,
            "repo": self.repo,
            "embedder": self.embedder,
        }
        kwargs.update(overrides)
        return DefaultPipelineRunner(**kwargs)

    # ------------------------------------------------------------------------- inspection

    def q(self, sql: str, **params: Any) -> list[Any]:
        with self.admin.connect() as conn:
            return list(conn.execute(text(sql), params).all())

    def pointer(self, scope: Scope) -> uuid.UUID | None:
        rows = self.q(
            "SELECT revision_id FROM document_publications WHERE document_id = :d",
            d=scope.document_id,
        )
        return rows[0][0] if rows else None

    def rev_state(self, revision_id: uuid.UUID) -> str | None:
        rows = self.q("SELECT state FROM knowledge_revisions WHERE id = :r", r=revision_id)
        return rows[0][0] if rows else None

    def checkpoints(self, revision_id: uuid.UUID) -> dict[str, Any]:
        rows = self.q(
            "SELECT stage, state, input_fingerprint, artifact_key, artifact_sha256, attempts"
            " FROM ingestion_checkpoints WHERE revision_id = :r",
            r=revision_id,
        )
        return {r[0]: r for r in rows}

    def search(self, scope: Scope, query: str) -> list[SearchHit]:
        hits: list[SearchHit] = self.repo.search_keyword(
            self.scope_of(scope.org_id), query, SearchFilters(frozenset({scope.workspace_id}))
        )
        return hits

    def hits_revisions(self, scope: Scope, query: str) -> set[uuid.UUID]:
        return {h.revision_id for h in self.search(scope, query)}


def eventually(fn: Callable[[], bool], *, timeout: float = 15.0) -> bool:
    """RushDB indexes asynchronously: poll for a positive condition (negatives are asserted
    directly, after the writes returned)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.2)
    return fn()


def document_text(keyword: str, paragraphs: int = 12) -> bytes:
    """Plain text with ``keyword`` in every paragraph; several chunks at a 300-char target."""
    body = [
        f"Paragraph {i} about {keyword}. "
        + "The quick brown fox jumps over the lazy dog near the river bank. " * 3
        for i in range(paragraphs)
    ]
    return "\n\n".join(body).encode()
