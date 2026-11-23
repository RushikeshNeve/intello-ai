"""The ingestion pipeline of one claimed job (M03.02.04, contract §2, §6, §9).

``extract -> chunk -> embed -> index -> validate -> publish``. Every stage is checkpointed in
PostgreSQL with an input fingerprint (``checkpoints.stage_fingerprint``); on a retry a stage whose
checkpoint is ``completed`` with an unchanged fingerprint and a verified artifact is **skipped**.
Nothing is searchable until the last stage's pointer swap (``knowledge/publication.py``).

Error mapping (the worker turns these into job states):

* ``ProcessingError`` ``unsupported_type``: result ``unsupported``
* ``ProcessingError`` ``no_text_extracted``: result ``no_text``
* any other permanent ``ProcessingError``: ``PermanentJobError(code)``
* transient embedding / index / object-store failure: ``RetryableJobError(code)``
* permanent embedding failure, space mismatch, bad state: ``PermanentJobError(code)``
* original does not match the recorded sha256: ``PermanentJobError("source_corrupt")``
* document or version deleted at any point: result ``succeeded`` / ``document_deleted``he
fencing on every write (``LeaseLost`` propagates untouched).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.ai.embeddings import EmbeddingError, EmbeddingProvider
from app.modules.ai.embeddings.cache import (
    CachingEmbedder,
    SqlEmbeddingCacheStore,
    get_cache_settings,
)
from app.modules.ingestion import artifacts as art
from app.modules.ingestion.artifacts import ArtifactStore
from app.modules.ingestion.checkpoints import StageRunner, fence
from app.modules.ingestion.chunking import chunk_document
from app.modules.ingestion.contracts import (
    UNSUPPORTED_CODES,
    Chunk,
    ProcessingError,
    SourceInfo,
)
from app.modules.ingestion.processors import Limits, extract
from app.modules.ingestion.runner_types import (
    ClaimedJob,
    JobContext,
    LeaseLost,
    PermanentJobError,
    PipelineResult,
    RetryableJobError,
)
from app.modules.ingestion.scan import service as scan
from app.modules.knowledge import indexing
from app.modules.knowledge.indexing import IndexValidationError, RevisionIdentity
from app.modules.knowledge.ports import (
    KnowledgeRepository,
    KnowledgeStoreError,
    KnowledgeStoreUnavailable,
    OrgScope,
    UnknownOrganization,
    validated_org_scope,
)
from app.modules.knowledge.publication import PublicationError, publish_revision
from app.modules.retrieval.cache import bump_corpus
from app.modules.storage.ports import ObjectNotFound, ObjectStore, StorageUnavailable

logger: Final = logging.getLogger(__name__)

_CHUNK_SLICE: Final = 100
_SNAKE: Final = re.compile(r"(?<!^)(?=[A-Z])")


class _DocumentDeleted(Exception):
    code = "document_deleted"


@dataclass(frozen=True, slots=True)
class _Source:
    pipeline_version: str
    embedding_space_id: str
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str
    version_number: int


def _snake(name: str) -> str:
    return _SNAKE.sub("_", name).lower()


class DefaultPipelineRunner:
    """``PipelineRunner`` loaded by the worker as
    ``app.modules.ingestion.pipeline:DefaultPipelineRunner`` (no-argument construction).

    Dependencies are built lazily from the environment (``ingestion.wiring``); tests pass fakes.
    """

    def __init__(
        self,
        *,
        uploads: ObjectStore | None = None,
        artifacts: ArtifactStore | None = None,
        repo: KnowledgeRepository | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self._uploads = uploads
        self._artifacts = artifacts
        self._repo = repo
        self._embedder = embedder

    # ------------------------------------------------------------------ lazy dependencies

    def _get_uploads(self) -> ObjectStore:
        if self._uploads is None:
            from app.modules.ingestion import wiring

            self._uploads = wiring.default_uploads_store()
        return self._uploads

    def _get_artifacts(self) -> ArtifactStore:
        if self._artifacts is None:
            from app.modules.ingestion import wiring

            self._artifacts = wiring.default_artifact_store()
        return self._artifacts

    def _get_repo(self) -> KnowledgeRepository:
        if self._repo is None:
            from app.modules.ingestion import wiring

            self._repo = wiring.default_repository()
        return self._repo

    def _get_embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            from app.modules.ingestion import wiring

            self._embedder = wiring.default_embedder()
        return self._embedder

    # ------------------------------------------------------------------------- entry point

    def run(self, claim: ClaimedJob, ctx: JobContext) -> PipelineResult:
        try:
            try:
                return self._run(claim, ctx)
            except _DocumentDeleted:
                self._discard_deleted(claim, ctx)
                return PipelineResult("succeeded", "document_deleted")
        except LeaseLost:
            raise
        except (RetryableJobError, PermanentJobError):
            raise
        except ProcessingError as exc:
            if exc.code in UNSUPPORTED_CODES:
                self._mark_failed(claim, ctx)
                return PipelineResult("unsupported", exc.code)
            if exc.code == "no_text_extracted":
                self._mark_failed(claim, ctx)
                return PipelineResult("no_text", exc.code)
            if exc.permanent:
                self._mark_failed(claim, ctx)
                raise PermanentJobError(exc.code, exc.detail) from exc
            raise RetryableJobError(exc.code, exc.detail) from exc
        except EmbeddingError as exc:
            code = _snake(type(exc).__name__)
            if exc.transient:
                raise RetryableJobError(code) from exc
            raise PermanentJobError(code) from exc
        except KnowledgeStoreUnavailable as exc:
            raise RetryableJobError("index_unavailable") from exc
        except KnowledgeStoreError as exc:
            if exc.code == "incomplete":
                raise RetryableJobError("index_incomplete") from exc
            raise PermanentJobError("index_error", exc.code or "") from exc
        except IndexValidationError as exc:
            raise RetryableJobError(exc.code) from exc
        except StorageUnavailable as exc:
            raise RetryableJobError("storage_unavailable") from exc
        except ObjectNotFound as exc:
            raise PermanentJobError("source_missing") from exc
        except PublicationError as exc:
            raise PermanentJobError(exc.code) from exc
        except UnknownOrganization as exc:
            raise PermanentJobError("organization_unavailable") from exc

    # -------------------------------------------------------------------------- the stages

    def _run(self, claim: ClaimedJob, ctx: JobContext) -> PipelineResult:
        settings = ctx.settings
        embedder = self._get_embedder()
        artifacts = self._get_artifacts()
        repo = self._get_repo()
        stages = StageRunner(claim, ctx, artifacts)

        with ctx.session_factory(claim.org_id) as session:
            fence(session, claim, stage="extract")
            src = self._load_source(session, claim)
            if src is None:
                raise _DocumentDeleted
            state = indexing.ensure_revision(
                session,
                RevisionIdentity(
                    claim.org_id,
                    claim.workspace_id,
                    claim.document_id,
                    claim.version_id,
                    claim.revision_id,
                    src.pipeline_version,
                    src.embedding_space_id,
                ),
            )
        if state in (indexing.STATE_SUPERSEDED, indexing.STATE_PURGED):
            return PipelineResult("succeeded", f"revision_{state}")
        if state == scan.STATE_QUARANTINED:  # parked until an admin releases it (M14.02)
            return PipelineResult("succeeded", "quarantined")
        spec = embedder.spec
        if spec.space_id != src.embedding_space_id:
            raise PermanentJobError("embedding_space_mismatch")
        ident = RevisionIdentity(
            claim.org_id,
            claim.workspace_id,
            claim.document_id,
            claim.version_id,
            claim.revision_id,
            src.pipeline_version,
            src.embedding_space_id,
        )
        pv = src.pipeline_version

        # 1. extract ------------------------------------------------------------------
        downloaded: list[bytes] = []

        def compute_extract() -> bytes:
            data = self._download(src, settings)
            downloaded.append(data)
            ctx.heartbeat()
            doc = extract(
                data,
                src.content_type,
                source=SourceInfo(
                    version_id=claim.version_id,
                    sha256=src.sha256,
                    content_type=src.content_type,
                    byte_size=len(data),
                ),
                limits=Limits(
                    max_pages=settings.INGEST_MAX_PDF_PAGES,
                    max_bytes=settings.INGEST_MAX_EXTRACT_BYTES,
                ),
            )
            return art.encode_extracted(doc)

        extracted = stages.artifact_stage(
            "extract",
            src.sha256,
            {
                "pipeline_version": pv,
                "content_type": src.content_type,
                "max_pages": settings.INGEST_MAX_PDF_PAGES,
                "max_bytes": settings.INGEST_MAX_EXTRACT_BYTES,
            },
            compute_extract,
        )
        if extracted.data is None:
            raise RuntimeError("stage output missing")
        self._alive(ctx, claim)

        # 1b. scan (M14.02): findings; quarantine mode stops here for a high-severity document ----
        if scan.run_scan_stage(
            ctx,
            claim,
            load_raw=lambda: downloaded[0] if downloaded else self._download(src, settings),
            load_doc=lambda: art.decode_extracted(extracted.data or b""),
            content_type=src.content_type,
        ):
            return PipelineResult("succeeded", "quarantined")
        self._alive(ctx, claim)

        # 2. chunk --------------------------------------------------------------------
        def compute_chunk() -> bytes:
            doc = art.decode_extracted(extracted.data or b"")
            chunks = chunk_document(
                doc,
                claim.revision_id,
                target_chars=settings.INGEST_CHUNK_TARGET_CHARS,
                overlap_chars=settings.INGEST_CHUNK_OVERLAP_CHARS,
                max_chunks=settings.INGEST_MAX_CHUNKS,
            )
            return art.encode_chunks(chunks)

        chunked = stages.artifact_stage(
            "chunk",
            extracted.sha256,
            {
                "pipeline_version": pv,
                "revision_id": str(claim.revision_id),
                "target_chars": settings.INGEST_CHUNK_TARGET_CHARS,
                "overlap_chars": settings.INGEST_CHUNK_OVERLAP_CHARS,
                "max_chunks": settings.INGEST_MAX_CHUNKS,
            },
            compute_chunk,
        )
        if chunked.data is None:
            raise RuntimeError("stage output missing")
        chunks = art.decode_chunks(chunked.data)
        self._alive(ctx, claim)

        # 3. embed --------------------------------------------------------------------
        def compute_embed() -> bytes:
            from app.modules.usage.wiring import Meter

            metered = Meter(ctx.session_factory, org_id=claim.org_id).embedder(embedder)
            return art.encode_embeddings(
                self._embed_all(
                    chunks,
                    metered or embedder,
                    settings.INGEST_EMBED_BATCH_SIZE,
                    ctx,
                    claim.org_id,
                ),
                spec.dimensions,
            )

        embedded = stages.artifact_stage(
            "embed",
            chunked.sha256,
            {
                "pipeline_version": pv,
                "embedding_space_id": src.embedding_space_id,
                "dimensions": spec.dimensions,
            },
            compute_embed,
        )
        if embedded.data is None:
            raise RuntimeError("stage output missing")
        vectors = art.decode_embeddings(embedded.data, len(chunks), spec.dimensions)
        self._alive(ctx, claim)

        # 4. index (unpublished) ----------------------------------------------------------
        scope = self._scope(ctx, claim)
        index_input = hashlib.sha256(f"{chunked.sha256}:{embedded.sha256}".encode()).hexdigest()

        def compute_index() -> None:
            def between(done: int, total: int) -> None:
                ctx.check_fence()
                ctx.heartbeat()

            indexing.index_chunks(
                repo,
                scope,
                ident,
                chunks,
                vectors,
                dimensions=spec.dimensions,
                slice_size=_CHUNK_SLICE,
                between_slices=between,
            )

        stages.effect_stage(
            "index",
            index_input,
            {
                "pipeline_version": pv,
                "embedding_space_id": src.embedding_space_id,
                "dimensions": spec.dimensions,
                "revision_id": str(claim.revision_id),
            },
            compute_index,
            verify=lambda: repo.count_chunks(scope, claim.revision_id) == len(chunks),
        )
        self._alive(ctx, claim)

        # 5. validate -> revision becomes ``ready`` ----------------------------------
        def compute_validate() -> None:
            indexing.validate_index(repo, scope, ident, chunks)
            with ctx.session_factory(claim.org_id) as session:
                fence(session, claim, stage="validate")
                indexing.mark_ready(
                    session,
                    claim.revision_id,
                    chunk_count=len(chunks),
                    content_fingerprint=indexing.content_fingerprint(chunks),
                )

        stages.effect_stage(
            "validate",
            chunked.sha256,
            {"pipeline_version": pv, "revision_id": str(claim.revision_id)},
            compute_validate,
        )

        # 6. publish (the barrier) ----------------------------------------------------
        outcomes: list[str] = []

        def compute_publish() -> None:
            outcome = publish_revision(
                ctx.session_factory, repo, claim, check_fence=ctx.check_fence
            )
            outcomes.append(outcome.status)
            bump_corpus(claim.org_id)  # M11.04: a published/superseded revision changes results
            if outcome.status == "document_deleted":
                raise _DocumentDeleted

        stages.effect_stage(
            "publish",
            index_input,
            {"pipeline_version": pv, "revision_id": str(claim.revision_id)},
            compute_publish,
        )
        detail = "published" if outcomes and outcomes[-1] != "superseded" else "superseded"
        return PipelineResult("succeeded", detail)

    # --------------------------------------------------------------------------- helpers

    def _scope(self, ctx: JobContext, claim: ClaimedJob) -> OrgScope:
        with ctx.session_factory(claim.org_id) as session:
            return validated_org_scope(session, claim.org_id)

    def _load_source(self, session: Session, claim: ClaimedJob) -> _Source | None:
        row = session.execute(
            text(
                "SELECT j.pipeline_version, j.embedding_space_id, v.object_key, v.sha256,"
                " v.size_bytes, v.content_type, v.version_number, d.deleted_at, v.purged_at"
                " FROM ingestion_jobs j"
                " JOIN document_versions v ON v.id = j.version_id AND v.document_id = j.document_id"
                " JOIN documents d ON d.id = j.document_id"
                " WHERE j.id = :job"
            ),
            {"job": claim.job_id},
        ).first()
        if row is None or row.deleted_at is not None or row.purged_at is not None:
            return None
        return _Source(
            row.pipeline_version,
            row.embedding_space_id,
            row.object_key,
            row.sha256,
            row.size_bytes,
            row.content_type,
            row.version_number,
        )

    def _alive(self, ctx: JobContext, claim: ClaimedJob) -> None:
        """Stop quietly when the document or version was deleted meanwhile."""
        with ctx.session_factory(claim.org_id) as session:
            fence(session, claim)
            if self._load_source(session, claim) is None:
                raise _DocumentDeleted

    def _download(self, src: _Source, settings: Any) -> bytes:
        """Fetch the original and prove it is the file that was finalized (sha256 and size)."""
        cap = int(settings.INGEST_MAX_EXTRACT_BYTES)
        if src.size_bytes > cap:
            raise ProcessingError("too_large", detail=f"{src.size_bytes} bytes exceeds {cap}")
        stream = self._get_uploads().get_object(src.object_key)
        digest = hashlib.sha256()
        buf = bytearray()
        try:
            for part in stream:
                buf += part
                digest.update(part)
                if len(buf) > min(cap, src.size_bytes):
                    raise PermanentJobError("source_corrupt", "original is larger than recorded")
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        if len(buf) != src.size_bytes or digest.hexdigest() != src.sha256:
            raise PermanentJobError("source_corrupt", "original does not match its checksum")
        return bytes(buf)

    def _embed_all(
        self,
        chunks: list[Chunk],
        embedder: EmbeddingProvider,
        batch_size: int,
        ctx: JobContext,
        org_id: uuid.UUID | None = None,
    ) -> list[list[float]]:
        if org_id is not None and get_cache_settings().EMBEDDING_CACHE_ENABLED:
            embedder = CachingEmbedder(
                embedder, SqlEmbeddingCacheStore(ctx.session_factory, org_id), org_id=org_id
            )
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), batch_size):
            ctx.check_fence()
            ctx.heartbeat()
            batch = [c.text for c in chunks[start : start + batch_size]]
            result = embedder.embed_documents_detailed(batch)
            if len(result.vectors) != len(batch):
                raise PermanentJobError("embedding_count_mismatch")
            vectors.extend(result.vectors)
        return vectors

    def _mark_failed(self, claim: ClaimedJob, ctx: JobContext) -> None:
        """Best effort: a terminal processing failure leaves the revision ``failed`` (not stuck)."""
        try:
            with ctx.session_factory(claim.org_id) as session:
                fence(session, claim)
                session.execute(
                    text(
                        "UPDATE knowledge_revisions SET state = 'failed'"
                        " WHERE id = :r AND state = 'building'"
                    ),
                    {"r": claim.revision_id},
                )
        except Exception:  # noqa: BLE001 - never mask the real outcome
            logger.warning("could not mark revision failed")

    def _discard_deleted(self, claim: ClaimedJob, ctx: JobContext) -> None:
        """The document is gone: remove whatever this job already wrote (idempotent)."""
        repo = self._get_repo()
        try:
            scope = self._scope(ctx, claim)
        except UnknownOrganization:
            return
        repo.delete_revision(scope, claim.revision_id)
        self._get_artifacts().delete_revision(claim.org_id, claim.revision_id)


def build_pipeline_runner(settings: Settings) -> DefaultPipelineRunner:
    """Dotted-path factory loaded by the worker runtime (``INGEST_PIPELINE_RUNNER``)."""
    del settings  # dependencies come from the environment (``ingestion.wiring``)
    return DefaultPipelineRunner()
