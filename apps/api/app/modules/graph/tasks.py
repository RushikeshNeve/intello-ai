"""``graph.extract_revision`` body (M05.01, contract m05 sections 1.6 and 3).

Triggered by the outbox event ``knowledge.revision.published`` on the ``maintenance`` queue; never
blocks publication and never fails a document: every "cannot build" outcome is a status string.

Fencing: PostgreSQL is the authority. The revision must be the published pointer (a) before any
work, (b) right before the graph write, and (c) after it; if the pointer moved in between, the graph
records of this revision are removed again and nothing stays visible. Writes are deterministic merge
writes, and the revision's previous graph records are replaced first, so re-running is idempotent.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules import prompts
from app.modules.ai.models.policy import ModelPolicy, PolicyDenied
from app.modules.ai.models.types import ChatModel
from app.modules.graph.claims import build_claims
from app.modules.graph.extract import EXTRACTOR_VERSION, SourceChunk, extract_chunks
from app.modules.graph.ontology import ENTITY_ACTIVE
from app.modules.graph.resolve import resolve_entities
from app.modules.ingestion.runner_types import WorkerSessions
from app.modules.knowledge.ports import (
    EntityRecord,
    GraphRepository,
    UnknownOrganization,
    validated_org_scope,
)
from app.modules.knowledge.publication import is_revision_published

logger: Final = logging.getLogger(__name__)

ModelFactory = Callable[[uuid.UUID], ChatModel]


@dataclass(frozen=True, slots=True)
class GraphOutcome:
    status: (
        str  # built | disabled | stale | no_chunks | model_unavailable | organization_unavailable
    )
    entities: int = 0
    claims: int = 0
    verified: int = 0


def _fence(
    session: Session, org_id: uuid.UUID, document_id: uuid.UUID, revision_id: uuid.UUID
) -> uuid.UUID | None:
    """The workspace id when the revision is still the published pointer, else ``None``."""
    if not is_revision_published(session, org_id, document_id, revision_id):
        return None
    return session.execute(
        text("SELECT workspace_id FROM knowledge_revisions WHERE id = :r AND document_id = :d"),
        {"r": revision_id, "d": document_id},
    ).scalar_one_or_none()


def extract_revision(
    sessions: WorkerSessions,
    repo: GraphRepository,
    model_factory: ModelFactory,
    settings: Settings,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> GraphOutcome:
    if not settings.GRAPH_ENABLED:
        return GraphOutcome("disabled")
    with sessions(org_id) as session:
        try:
            scope = validated_org_scope(session, org_id)
        except UnknownOrganization:
            return GraphOutcome("organization_unavailable")
        workspace_id = _fence(session, org_id, document_id, revision_id)
    if workspace_id is None:
        return GraphOutcome("stale")

    raw_chunks = repo.read_chunks(scope, revision_id, limit=settings.GRAPH_MAX_CHUNKS_PER_REVISION)
    chunks = [
        SourceChunk(
            c.chunk_id,
            c.chunk_index,
            c.text,
            c.workspace_id,
            c.document_id,
            c.version_id,
            c.revision_id,
        )
        for c in raw_chunks
        if c.workspace_id == workspace_id and c.document_id == document_id
    ]
    if not chunks:
        return GraphOutcome("no_chunks")
    try:
        model = model_factory(org_id)
    except PolicyDenied:
        return GraphOutcome("model_unavailable")
    with sessions(org_id) as session:
        prompt = prompts.resolve("graph_extract", org_id, session)
    # M12.01: a custom prompt is recorded on every entity/claim via the extractor version.
    extractor_version = (
        EXTRACTOR_VERSION
        if prompt.sha256 == prompts.sha256_of(prompts.default_body("graph_extract"))
        else f"{EXTRACTOR_VERSION}+p{prompt.sha256[:12]}"
    )
    extraction = extract_chunks(
        model,
        chunks,
        batch_size=settings.GRAPH_EXTRACT_BATCH_CHUNKS,
        max_entities_per_chunk=settings.GRAPH_MAX_ENTITIES_PER_CHUNK,
        instruction=prompt.body,
    )

    with sessions(org_id) as session:
        if _fence(session, org_id, document_id, revision_id) is None:
            return GraphOutcome("stale")
        resolved = resolve_entities(session, org_id, workspace_id, extraction.entities.values())
    built = build_claims(
        extraction,
        resolved,
        chunks,
        org_id=org_id,
        workspace_id=workspace_id,
        document_id=document_id,
        revision_id=revision_id,
        extractor_version=extractor_version,
    )

    repo.delete_graph_revision(scope, revision_id)  # replace, never accumulate, per revision
    repo.upsert_entities(
        scope,
        [
            EntityRecord(
                id=e.id,
                org_id=org_id,
                workspace_id=workspace_id,
                type=e.type,
                canonical_name=e.canonical_name,
                aliases=e.aliases,
                entity_state=ENTITY_ACTIVE,
                extractor_version=extractor_version,
                source_revision_id=revision_id,
            )
            for e in resolved.entities
        ],
    )
    repo.upsert_claims(scope, built.claims, built.evidence, published=True)

    with sessions(org_id) as session:
        still = _fence(session, org_id, document_id, revision_id) is not None
    if not still:  # superseded/deleted while writing: leave nothing behind
        repo.delete_graph_revision(scope, revision_id)
        return GraphOutcome("stale")
    return GraphOutcome("built", len(resolved.entities), len(built.claims), built.verified)


# ------------------------------------------------------------------------- default wiring


def default_model_factory(sessions: WorkerSessions) -> ModelFactory:
    """The org's chat model through the M04 gateway (local by default; policy fails closed)."""

    def factory(org_id: uuid.UUID) -> ChatModel:
        from app.modules.ai.gateway import get_chat_model
        from app.modules.ai.models.policy import InMemoryPolicySource
        from app.modules.ai.models.settings import ChatSettings
        from app.modules.ai.policy_store import load_policy

        with sessions(org_id) as session:
            stored = load_policy(session, org_id)
        source = InMemoryPolicySource(
            {org_id: ModelPolicy(stored.allow_external, frozenset(stored.allowed_providers))}
        )
        return get_chat_model(source, org_id, ChatSettings())

    return factory


def run_default(org_id: str, document_id: str, revision_id: str) -> str:
    """Celery entry: build the real dependencies lazily and run :func:`extract_revision`."""
    from app.infrastructure.rushdb.graph import RushDbGraphRepository
    from app.infrastructure.rushdb.settings import RushDbSettings
    from app.modules.ingestion import wiring
    from app.workers.settings import get_worker_settings

    settings = get_worker_settings()
    if not settings.GRAPH_ENABLED:
        return "disabled"
    sessions = wiring.default_worker_sessions(settings)
    repo = RushDbGraphRepository(RushDbSettings())
    try:
        outcome = extract_revision(
            sessions,
            repo,
            default_model_factory(sessions),
            settings,
            uuid.UUID(org_id),
            uuid.UUID(document_id),
            uuid.UUID(revision_id),
        )
    finally:
        repo.close()
    logger.info("graph.extract_revision %s", outcome.status)
    return outcome.status
