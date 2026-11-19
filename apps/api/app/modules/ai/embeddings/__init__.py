"""Local embedding adapter (M03.02.05): registry, OpenAI-compatible client, fake."""

from app.modules.ai.embeddings.client import (
    EmbeddingClient,
    build_embedding_client,
    estimate_tokens,
    is_local_host,
)
from app.modules.ai.embeddings.errors import (
    EmbeddingConfigurationError,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingInputInvalid,
    EmbeddingModelMismatch,
    EmbeddingRequestRejected,
    EmbeddingResponseInvalid,
    EmbeddingUnavailable,
)
from app.modules.ai.embeddings.fake import FakeEmbeddingClient, hash_embed
from app.modules.ai.embeddings.ports import EmbeddingBatchResult, EmbeddingProvider, EmbeddingUsage
from app.modules.ai.embeddings.registry import (
    DEFAULT_SPEC,
    EmbeddingModelSpec,
    spec_from_settings,
)
from app.modules.ai.embeddings.settings import EmbeddingSettings

__all__ = [
    "DEFAULT_SPEC",
    "EmbeddingBatchResult",
    "EmbeddingClient",
    "EmbeddingConfigurationError",
    "EmbeddingDimensionMismatch",
    "EmbeddingError",
    "EmbeddingInputInvalid",
    "EmbeddingModelMismatch",
    "EmbeddingModelSpec",
    "EmbeddingProvider",
    "EmbeddingRequestRejected",
    "EmbeddingResponseInvalid",
    "EmbeddingSettings",
    "EmbeddingUnavailable",
    "EmbeddingUsage",
    "FakeEmbeddingClient",
    "build_embedding_client",
    "estimate_tokens",
    "hash_embed",
    "is_local_host",
    "spec_from_settings",
]
