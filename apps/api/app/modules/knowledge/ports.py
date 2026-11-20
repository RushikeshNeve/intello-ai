"""Knowledge-index port (M03.03.01, contract docs/architecture/m03-contracts.md section 8).

The port is the only thing the rest of the API knows about the derived knowledge index (RushDB).
Implementations: ``app.infrastructure.rushdb.repository.RushDbKnowledgeRepository`` (real, httpx)
and ``app.infrastructure.rushdb.memory.InMemoryKnowledgeRepository`` (tests of other modules).

Rules every implementation follows (DC-009, DC-023, spike section 7):

* **Tenant = organization.** Every method takes an :class:`OrgScope`. The only way to get one is
  :func:`validated_org_scope`, which proves in PostgreSQL that the organization exists and is not
  deleted (RushDB would silently auto-register an unknown scope as an empty tenant). A scope is
  never built from a request value.
* **Searches are always filtered.** ``search_*`` require :class:`SearchFilters` (a non-empty set of
  authorized workspace ids) and the implementation itself adds ``workspace_id IN (...)`` and
  ``published = true``. No caller composes a predicate.
* **PostgreSQL stays the authority.** ``published`` is a pre-filter only; callers must revalidate
  every hit's ``revision_id`` against ``document_publications`` before using it.
* **Idempotent writes.** Chunk ids are deterministic (``chunk_id_for``); writing a chunk twice
  yields one record, and re-writing a chunk never un-publishes it.
* Errors carry an operation name and a short code only: never the token, the URL, or chunk text.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.ingestion.ids import chunk_id_for

# --------------------------------------------------------------------------------- errors


class KnowledgeStoreError(Exception):
    """Base class of every error raised by a :class:`KnowledgeRepository`.

    ``code`` is the provider's short error code when there is one (``conflict``,
    ``invalid_request``...). The message never contains the token, the URL or any chunk text.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class KnowledgeStoreUnavailable(KnowledgeStoreError):
    """The index is unreachable or failing: network errors, timeouts, 5xx, rejected credentials.

    Callers answer 503 / retry the job later; it never means "no results".
    """


class UnknownOrganization(Exception):
    """The organization does not exist (or is deleted, or is not visible to the session)."""


# ----------------------------------------------------------------------------- constants

MAX_SEARCH_LIMIT: Final = 200
MAX_QUERY_CHARS: Final = 2000
MAX_CHUNK_TEXT_CHARS: Final = 200_000
MAX_JSON_PROPERTY_CHARS: Final = 100_000
MAX_WORKSPACES_PER_FILTER: Final = 1000
MAX_UPSERT_BATCH: Final = 100

_SPACE_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def validate_space_id(space_id: str) -> str:
    """An embedding-space id is used inside a RushDB index name, so keep it to a safe alphabet."""
    if not _SPACE_RE.fullmatch(space_id):
        raise ValueError("embedding_space_id must be 1-64 chars of [A-Za-z0-9_-]")
    return space_id


def validate_vector(vector: Sequence[float], dimensions: int | None = None) -> None:
    """Reject a wrong length or any non-finite value (NaN, +-inf) before it reaches the index."""
    if dimensions is not None and len(vector) != dimensions:
        raise ValueError(f"vector has {len(vector)} dimensions, the index has {dimensions}")
    if len(vector) == 0:
        raise ValueError("vector must not be empty")
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("vector values must be numbers")
        if not math.isfinite(value):
            raise ValueError("vector values must be finite")


def validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
    return limit


def validate_query_text(query: str) -> str:
    stripped = query.strip()
    if not stripped:
        raise ValueError("query must not be empty")
    if len(stripped) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be at most {MAX_QUERY_CHARS} characters")
    return stripped


# ------------------------------------------------------------------------------------ scope

_MINT: Final = object()


@dataclass(frozen=True, slots=True)
class OrgScope:
    """The tenant of one call: organization ``org_id``. Obtain it with :func:`validated_org_scope`.

    Constructing it directly raises ``TypeError``: a scope can therefore never be built from a
    request value by accident (the guard is a convention-level barrier, not a security boundary;
    the real boundary is that only :func:`validated_org_scope` knows the mint token).
    """

    org_id: uuid.UUID
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT or not isinstance(self.org_id, uuid.UUID):
            raise TypeError(
                "OrgScope can only be created with validated_org_scope(session, org_id)"
            )

    @property
    def tenant(self) -> str:
        """The RushDB tenant scope string (header ``X-RushDB-Tenant`` and ``context.scope``)."""
        return f"org:{self.org_id}"


def validated_org_scope(session: Session, org_id: uuid.UUID) -> OrgScope:
    """Return the scope of ``org_id`` after proving in PostgreSQL that the org is live.

    Runs one query through the caller's session (so row-level security applies exactly as for the
    caller). Raises :class:`UnknownOrganization` when the org does not exist, is soft-deleted, or is
    not visible to the session. Call it right before using the repository, from trusted code with
    an org id derived server-side (path resolution, job row), never from a request body.
    """
    if not isinstance(org_id, uuid.UUID):
        raise UnknownOrganization("organization id must be a UUID")
    found = session.execute(
        text("SELECT 1 FROM organizations WHERE id = :id AND deleted_at IS NULL"),
        {"id": org_id},
    ).first()
    if found is None:
        raise UnknownOrganization("organization not found")
    return OrgScope(org_id, _mint=_MINT)


# -------------------------------------------------------------------------------------- DTOs


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """One chunk to index. ``chunk_id`` must equal ``chunk_id_for(revision_id, chunk_index)``."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID
    chunk_index: int
    chunk_id: uuid.UUID
    text: str
    token_estimate: int
    content_hash: str
    locators: Sequence[Mapping[str, Any]]
    heading_path: Sequence[str]
    page_start: int | None
    page_end: int | None
    embedding_space_id: str

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be >= 0")
        if self.chunk_id != chunk_id_for(self.revision_id, self.chunk_index):
            raise ValueError("chunk_id must be chunk_id_for(revision_id, chunk_index)")
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if len(self.text) > MAX_CHUNK_TEXT_CHARS:
            raise ValueError(f"text must be at most {MAX_CHUNK_TEXT_CHARS} characters")
        if self.token_estimate < 0:
            raise ValueError("token_estimate must be >= 0")
        if not self.content_hash:
            raise ValueError("content_hash must not be empty")
        validate_space_id(self.embedding_space_id)
        for page in (self.page_start, self.page_end):
            if page is not None and page < 1:
                raise ValueError("pages are 1-based")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("page_end must be >= page_start")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("page_start and page_end are both set or both None")
        # Serialized once to prove they are JSON; also bounds the stored size.
        for label, value in (("locators", self.locators), ("heading_path", self.heading_path)):
            try:
                encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be JSON-serializable") from None
            if len(encoded) > MAX_JSON_PROPERTY_CHARS:
                raise ValueError(f"{label} is too large")
        if not all(isinstance(h, str) for h in self.heading_path):
            raise ValueError("heading_path must be a list of strings")


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """The mandatory filter of every search: the workspaces the caller may read (non-empty)."""

    workspace_ids: frozenset[uuid.UUID]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_ids, frozenset):
            raise TypeError("workspace_ids must be a frozenset of UUIDs")
        if not self.workspace_ids:
            raise ValueError("workspace_ids must not be empty (a search without scope is refused)")
        if len(self.workspace_ids) > MAX_WORKSPACES_PER_FILTER:
            raise ValueError(f"at most {MAX_WORKSPACES_PER_FILTER} workspaces per search")
        if not all(isinstance(w, uuid.UUID) for w in self.workspace_ids):
            raise TypeError("workspace_ids must contain UUIDs")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A candidate chunk. Callers revalidate ``revision_id`` against PostgreSQL before use."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID
    workspace_id: uuid.UUID
    score: float
    text: str
    locators: list[dict[str, Any]]
    heading_path: list[str]
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True, slots=True)
class DeleteCounts:
    """What a purge removed (``evidence`` and ``claims`` are the M05 graph records)."""

    chunks: int
    evidence: int = 0
    claims: int = 0


# ------------------------------------------------------------------------------------ port


@runtime_checkable
class KnowledgeRepository(Protocol):
    """Tenant-scoped chunk store and search (see the module docstring for the shared rules)."""

    def ensure_indexes(self, scope: OrgScope, embedding_space_id: str, dimensions: int) -> None:
        """Create (idempotent) vector index ``chunk-emb-<space>`` and BM25 index ``chunk-text``.

        Returns once the text index is ``ready``. Re-declaring a space with other dimensions raises
        :class:`KnowledgeStoreError`.
        """
        ...

    def upsert_chunks(
        self,
        scope: OrgScope,
        chunks: Sequence[ChunkRecord],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        """Write chunks (``published=false`` for new ones) with their vectors; returns the count.

        ``vectors[i]`` belongs to ``chunks[i]``. All chunks share one embedding space whose index
        was ensured. Idempotent: the same chunk twice is one record, and a chunk that is already
        published stays published. Raises ``ValueError`` on a wrong vector length / non-finite value
        / chunk of another org.
        """
        ...

    def count_chunks(
        self, scope: OrgScope, revision_id: uuid.UUID, published: bool | None = None
    ) -> int: ...

    def list_chunk_ids(self, scope: OrgScope, revision_id: uuid.UUID) -> list[uuid.UUID]:
        """Chunk ids of a revision ordered by ``chunk_index``."""
        ...

    def set_published(self, scope: OrgScope, revision_id: uuid.UUID, flag: bool) -> int:
        """Set the ``published`` flag of every chunk of the revision; returns how many changed."""
        ...

    def delete_revision(self, scope: OrgScope, revision_id: uuid.UUID) -> DeleteCounts:
        """Purge one revision's records. Idempotent (a second call returns zero counts)."""
        ...

    def delete_document(self, scope: OrgScope, document_id: uuid.UUID) -> DeleteCounts:
        """Purge every revision of a document. Idempotent."""
        ...

    def search_keyword(
        self, scope: OrgScope, query: str, filters: SearchFilters, *, limit: int = 10
    ) -> list[SearchHit]:
        """BM25 over published chunks of the filtered workspaces, best first."""
        ...

    def search_vector(
        self,
        scope: OrgScope,
        embedding_space_id: str,
        vector: Sequence[float],
        filters: SearchFilters,
        *,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Cosine nearest neighbours among published chunks of the filtered workspaces."""
        ...

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
        """Reciprocal-rank fusion (k=60) of the BM25 leg and the vector leg."""
        ...


def vector_index_name(embedding_space_id: str) -> str:
    return f"chunk-emb-{validate_space_id(embedding_space_id)}"


TEXT_INDEX_NAME: Final = "chunk-text"


# ------------------------------------------------------------------------- graph (M05)


@dataclass(frozen=True, slots=True)
class GraphChunk:
    """A chunk as read back for extraction (the index is rebuildable from artifacts)."""

    chunk_id: uuid.UUID
    chunk_index: int
    text: str
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class EntityRecord:
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    type: str
    canonical_name: str
    aliases: Sequence[str]
    entity_state: str
    extractor_version: str
    source_revision_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    revision_id: uuid.UUID
    subject_id: uuid.UUID
    predicate: str
    object_id: uuid.UUID | None
    object_text: str | None
    confidence: float
    verification: str  # "verified" | "unverified"
    extractor_version: str
    published: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: uuid.UUID
    claim_id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    chunk_id: uuid.UUID
    revision_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    excerpt: str


@runtime_checkable
class GraphRepository(Protocol):
    """Graph projection store (RushDB ``Entity``/``Claim``/``Evidence``), same tenant rules as
    :class:`KnowledgeRepository`: a scope from :func:`validated_org_scope`, deterministic ids,
    merge writes. Claims are flagged ``published`` together with their revision's chunks
    (``KnowledgeRepository.set_published``); ``delete_revision``/``delete_document`` remove them.

    Layout notes for readers (traversal): relationships are stored as properties, not RushDB edges:
    ``Claim.subject_id``, ``Claim.object_id`` (absent for a literal ``object_text``) and
    ``Evidence.claim_id``; ``Entity.aliases`` is a JSON-encoded list of strings.
    """

    def read_chunks(
        self, scope: OrgScope, revision_id: uuid.UUID, *, limit: int
    ) -> list[GraphChunk]:
        """Chunks of the revision ordered by ``chunk_index`` (at most ``limit``)."""
        ...

    def upsert_entities(self, scope: OrgScope, entities: Sequence[EntityRecord]) -> int: ...

    def upsert_claims(
        self,
        scope: OrgScope,
        claims: Sequence[ClaimRecord],
        evidence: Sequence[EvidenceRecord],
        *,
        published: bool,
    ) -> int:
        """Write claims (with the given ``published`` flag) and their evidence; returns claims."""
        ...

    def list_claims(
        self, scope: OrgScope, revision_id: uuid.UUID, *, verification: str | None = None
    ) -> list[ClaimRecord]: ...

    def list_evidence(self, scope: OrgScope, claim_id: uuid.UUID) -> list[EvidenceRecord]: ...

    def list_entities(self, scope: OrgScope, workspace_id: uuid.UUID) -> list[EntityRecord]: ...

    def delete_graph_revision(self, scope: OrgScope, revision_id: uuid.UUID) -> int:
        """Remove only the claims/evidence of a revision (chunks stay); returns claims removed."""
        ...

    def remap_entity(self, scope: OrgScope, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
        """After an accepted merge: mark ``from_id`` merged and point its claims at ``to_id``."""
        ...
