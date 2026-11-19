"""Embedding model registry (M03.02.05, contract section 7, ADR-020).

An ``EmbeddingModelSpec`` fully describes how text becomes vectors. The ``space_id`` identifies the
embedding space: vectors are only comparable inside one space, so a different model, dimension
count, prefix or spec version is a new space (new revision, new vector index, re-embedding).

``max_input_chars`` and ``normalize`` are deliberately NOT part of the space id (the frozen formula
has five inputs): truncation length is a safety cap and unit-length scaling does not change cosine
ranking. Changing what the vectors *mean* requires bumping ``spec_version``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol

from app.modules.ingestion.ids import compute_embedding_space_id

PROVIDER_OPENAI_COMPATIBLE: Final = "openai_compatible"
_PROVIDERS: Final = frozenset({PROVIDER_OPENAI_COMPATIBLE})

DEFAULT_MODEL_ID: Final = "google/embedding-gemma-300m"
DEFAULT_DIMENSIONS: Final = 768
DEFAULT_QUERY_PREFIX: Final = "task: search result | query: "
DEFAULT_DOCUMENT_PREFIX: Final = "title: none | text: "
DEFAULT_SPEC_VERSION: Final = 1
# The model context is 2048 tokens. 6000 chars is ~1500 tokens at 4 chars/token and still < 2048
# at 3 chars/token, with room for the prefix. The chunker's hard max is 2400 chars, so chunks are
# never truncated in practice; this protects against oversized inputs.
DEFAULT_MAX_INPUT_CHARS: Final = 6000
MAX_DIMENSIONS: Final = 8192


@dataclass(frozen=True)
class EmbeddingModelSpec:
    model_id: str
    dimensions: int
    query_prefix: str
    document_prefix: str
    max_input_chars: int
    spec_version: int
    normalize: bool = True
    provider: str = PROVIDER_OPENAI_COMPATIBLE

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError(f"unsupported embedding provider {self.provider!r}")
        if not self.model_id.strip():
            raise ValueError("model_id must not be blank")
        if not 1 <= self.dimensions <= MAX_DIMENSIONS:
            raise ValueError(f"dimensions must be between 1 and {MAX_DIMENSIONS}")
        if self.max_input_chars < 1:
            raise ValueError("max_input_chars must be >= 1")
        if self.spec_version < 1:
            raise ValueError("spec_version must be >= 1")

    @property
    def space_id(self) -> str:
        return compute_embedding_space_id(
            self.model_id,
            self.dimensions,
            self.query_prefix,
            self.document_prefix,
            self.spec_version,
        )


DEFAULT_SPEC: Final = EmbeddingModelSpec(
    model_id=DEFAULT_MODEL_ID,
    dimensions=DEFAULT_DIMENSIONS,
    query_prefix=DEFAULT_QUERY_PREFIX,
    document_prefix=DEFAULT_DOCUMENT_PREFIX,
    max_input_chars=DEFAULT_MAX_INPUT_CHARS,
    spec_version=DEFAULT_SPEC_VERSION,
)


class _SpecSettings(Protocol):
    AI_EMBEDDING_MODEL: str
    AI_EMBEDDING_DIMENSIONS: int


def spec_from_settings(settings: _SpecSettings) -> EmbeddingModelSpec:
    """Build the spec from ``AI_EMBEDDING_*`` settings.

    Works with ``EmbeddingSettings`` and with the shared ``Settings`` (duck-typed). Fields a
    settings object does not have fall back to the defaults above.
    """

    def get(name: str, default: Any) -> Any:
        value = getattr(settings, name, None)
        return default if value is None else value

    return EmbeddingModelSpec(
        model_id=settings.AI_EMBEDDING_MODEL,
        dimensions=settings.AI_EMBEDDING_DIMENSIONS,
        query_prefix=get("AI_EMBEDDING_QUERY_PREFIX", DEFAULT_QUERY_PREFIX),
        document_prefix=get("AI_EMBEDDING_DOCUMENT_PREFIX", DEFAULT_DOCUMENT_PREFIX),
        max_input_chars=get("AI_EMBEDDING_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS),
        spec_version=get("AI_EMBEDDING_SPEC_VERSION", DEFAULT_SPEC_VERSION),
        normalize=get("AI_EMBEDDING_NORMALIZE", True),
    )
