"""Deterministic fake embedding client for fast tests of other modules (no network, no model).

Algorithm (feature hashing over a bag of words). It is implemented a second time in
``tests/e2e/support/embedding-stub.mjs``; a golden-file test keeps both in lockstep:

1. ``text`` is lower-cased (``str.lower()`` / ``toLowerCase()``).
2. Tokens are the maximal runs of Unicode letters or numbers (Python ``[^\\W_]+``, JavaScript
   ``[\\p{L}\\p{N}]+``). Identical for ASCII; non-ASCII is best-effort identical.
3. For each token: ``h = sha256(token.encode("utf-8"))``; the bucket is
   ``int.from_bytes(h[0:4], "big") % dimensions`` and the sign is ``+1`` if ``h[4]`` is even else
   ``-1``. The token adds its sign to that bucket (so repeated words weigh more).
4. No tokens at all (e.g. ``"!!!"``), or an all-zero sum: the vector is the unit vector on bucket 0.
5. The vector is divided by its L2 norm (the sums are small integers, so this is exact in float64).

Texts that share words therefore have a higher cosine similarity than unrelated texts. Like the
real client, the fake applies the spec's prefix and ``max_input_chars`` truncation FIRST and hashes
the prefixed text, so prefix tokens are part of every vector exactly as a server would see them.
Blank input is rejected like in the real client; errors can be injected with ``fail_next``.
"""

from __future__ import annotations

import hashlib
import math
import re
from types import TracebackType

from app.modules.ai.embeddings.client import estimate_tokens
from app.modules.ai.embeddings.errors import EmbeddingError, EmbeddingInputInvalid
from app.modules.ai.embeddings.ports import EmbeddingBatchResult, EmbeddingUsage
from app.modules.ai.embeddings.registry import DEFAULT_SPEC, EmbeddingModelSpec

_TOKEN_RE = re.compile(r"[^\W_]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def hash_embed(text: str, dimensions: int) -> list[float]:
    """The feature-hashing embedding of ``text`` (see the module docstring)."""
    if dimensions < 1:
        raise ValueError("dimensions must be >= 1")
    sums = [0] * dimensions
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[0:4], "big") % dimensions
        sums[bucket] += 1 if digest[4] % 2 == 0 else -1
    norm = math.sqrt(sum(s * s for s in sums))
    if norm == 0.0:
        unit = [0.0] * dimensions
        unit[0] = 1.0
        return unit
    return [s / norm for s in sums]


class FakeEmbeddingClient:
    """Same interface as ``EmbeddingClient`` (``EmbeddingProvider``)."""

    def __init__(self, spec: EmbeddingModelSpec = DEFAULT_SPEC) -> None:
        self._spec = spec
        self._usage = EmbeddingUsage()
        self._failures: list[EmbeddingError] = []
        self.document_calls = 0
        self.query_calls = 0
        self.closed = False

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    @property
    def usage(self) -> EmbeddingUsage:
        return self._usage

    def fail_next(self, error: EmbeddingError, times: int = 1) -> None:
        """Make the next ``times`` calls raise ``error`` (e.g. ``EmbeddingUnavailable``)."""
        self._failures.extend([error] * times)

    def embed_documents_detailed(self, texts: list[str]) -> EmbeddingBatchResult:
        self.document_calls += 1
        return self._embed(texts, self._spec.document_prefix)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents_detailed(texts).vectors

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._embed([text], self._spec.query_prefix).vectors[0]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeEmbeddingClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _embed(self, texts: list[str], prefix: str) -> EmbeddingBatchResult:
        if self._failures:
            raise self._failures.pop(0)
        if not texts:
            return EmbeddingBatchResult([], (), EmbeddingUsage())
        prepared: list[str] = []
        truncated: list[int] = []
        for i, text in enumerate(texts):
            if not text.strip():
                raise EmbeddingInputInvalid(f"input {i} is blank")
            if len(text) > self._spec.max_input_chars:
                text = text[: self._spec.max_input_chars]
                truncated.append(i)
            prepared.append(prefix + text)
        usage = EmbeddingUsage(
            requests=1,
            inputs=len(prepared),
            estimated_tokens=sum(estimate_tokens(len(t)) for t in prepared),
            truncated_inputs=len(truncated),
        )
        self._usage = self._usage + usage
        vectors = [hash_embed(t, self._spec.dimensions) for t in prepared]
        return EmbeddingBatchResult(vectors, tuple(truncated), usage)
