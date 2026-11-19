"""Typed embedding errors (M03 contract section 6: transient vs permanent).

Every error exposes ``.transient``: the job runner retries transient ones with backoff and fails
permanent ones immediately. Messages are fixed, short and safe: they never contain input text,
vectors, the API key, a response body or a URL (section 12).
"""

from __future__ import annotations


class EmbeddingError(Exception):
    """Base class. ``transient`` tells the caller whether retrying later can help."""

    transient: bool = False


class EmbeddingUnavailable(EmbeddingError):
    """Server unreachable, timing out, overloaded (429) or failing (5xx) after the retry budget."""

    transient = True

    def __init__(self, reason: str, *, attempts: int = 1) -> None:
        super().__init__(f"embedding server unavailable after {attempts} attempt(s): {reason}")
        self.reason = reason
        self.attempts = attempts


class EmbeddingRequestRejected(EmbeddingError):
    """The server refused the request (4xx other than 429, or an unexpected 3xx). Never retried."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"embedding server rejected the request (HTTP {status_code})")
        self.status_code = status_code


class EmbeddingResponseInvalid(EmbeddingError):
    """The server answered 2xx but the body is not a valid embeddings response. Nothing is used."""


class EmbeddingDimensionMismatch(EmbeddingResponseInvalid):
    """A returned vector does not have exactly the spec's number of dimensions."""

    def __init__(self, expected: int, actual: int, *, index: int) -> None:
        super().__init__(f"vector {index} has {actual} dimensions, expected {expected}")
        self.expected = expected
        self.actual = actual
        self.index = index


class EmbeddingModelMismatch(EmbeddingResponseInvalid):
    """The server says it answered with a different model than the one requested."""

    def __init__(self, expected: str, actual: str) -> None:
        # Model ids are configuration, not user content; truncated to keep messages bounded.
        super().__init__(f"server answered with model {actual[:80]!r}, expected {expected[:80]!r}")
        self.expected = expected
        self.actual = actual


class EmbeddingInputInvalid(EmbeddingError):
    """The caller passed unusable input (blank text, non-string). A programming/data error."""


class EmbeddingConfigurationError(EmbeddingError):
    """Settings are unusable or unsafe (bad URL, external provider not allowed). Not retried."""
