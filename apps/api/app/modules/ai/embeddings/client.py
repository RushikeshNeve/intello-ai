"""Sync client for any OpenAI-compatible ``POST {AI_BASE_URL}/embeddings`` server (M03.02.05).

Behaviour (contract section 7, retry classes section 6):

* Document texts get ``spec.document_prefix``, queries ``spec.query_prefix``. Each text is first cut
  to ``spec.max_input_chars`` characters (``text[:max_input_chars]``, deterministic; the prefix is
  added afterwards) and the cut inputs are reported. Inputs go out in batches of ``batch_size``.
* Blank text (empty or whitespace only) is REJECTED with ``EmbeddingInputInvalid`` rather than
  silently replaced: the chunker never emits blanks, so one reaching here is a bug that must not be
  hidden behind a meaningless vector.
* Everything the server returns is validated before anything is handed back: HTTP status, JSON
  shape, one vector per input (restored to input order by ``index``), exactly ``spec.dimensions``
  finite floats each, a non-zero norm, and (if the server names a model) the requested model.
  Any violation raises and **no vectors are returned**, also not from earlier batches.
* Retries: at most ``max_attempts`` tries per request with exponential backoff and jitter; timeouts,
  connection errors, 429 and 5xx are retried and finally raised as ``EmbeddingUnavailable``
  (transient). Other 4xx and every validation failure are permanent and never retried.
* Safety: errors and logs never contain texts, vectors, response bodies, the API key or the URL.
  A non-local base URL is refused unless ``allow_external`` (``AI_EXTERNAL_PROVIDERS_ENABLED``);
  the tenant-policy gate itself arrives in M04.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.core.tracing import httpx_request_hook
from app.modules.ai import egress
from app.modules.ai.embeddings.errors import (
    EmbeddingConfigurationError,
    EmbeddingDimensionMismatch,
    EmbeddingInputInvalid,
    EmbeddingModelMismatch,
    EmbeddingRequestRejected,
    EmbeddingResponseInvalid,
    EmbeddingUnavailable,
)
from app.modules.ai.embeddings.ports import EmbeddingBatchResult, EmbeddingUsage
from app.modules.ai.embeddings.registry import EmbeddingModelSpec, spec_from_settings
from app.modules.ai.embeddings.settings import EmbeddingSettings

logger = logging.getLogger("ukos.ai.embeddings")

BACKOFF_BASE_SECONDS: Final = 0.5
BACKOFF_CAP_SECONDS: Final = 8.0


def estimate_tokens(char_count: int) -> int:
    """Client-side token estimate, ``ceil(chars / 4)`` (servers report 0)."""
    return -(-char_count // 4)


def is_local_host(host: str) -> bool:
    """Local = localhost, ``*.localhost``, loopback IPs, host.docker.internal, or a dot-free
    docker-compose service name."""
    h = host.strip().lower().strip("[]").rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost") or h == "host.docker.internal":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return "." not in h and ":" not in h


def validate_base_url(base_url: str, *, allow_external: bool) -> str:
    """Normalise the base URL (no trailing slash) and enforce the local-only guard."""
    parts = urlsplit(base_url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EmbeddingConfigurationError("AI_BASE_URL must be an http(s) URL with a host")
    if parts.username or parts.password:
        raise EmbeddingConfigurationError("AI_BASE_URL must not embed credentials")
    if parts.query or parts.fragment:
        raise EmbeddingConfigurationError("AI_BASE_URL must not have a query or fragment")
    if not allow_external and not is_local_host(parts.hostname):
        raise EmbeddingConfigurationError(
            "AI_BASE_URL is not a local host and AI_EXTERNAL_PROVIDERS_ENABLED is false"
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class EmbeddingClient:
    def __init__(
        self,
        spec: EmbeddingModelSpec,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        connect_timeout_seconds: float = 5.0,
        batch_size: int = 16,
        max_attempts: int = 3,
        allow_external: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if batch_size < 1:
            raise EmbeddingConfigurationError("batch_size must be >= 1")
        if max_attempts < 1:
            raise EmbeddingConfigurationError("max_attempts must be >= 1")
        self._spec = spec
        self._base_url = validate_base_url(base_url, allow_external=allow_external)
        self._url = f"{self._base_url}/embeddings"
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._sleep = sleeper
        self._jitter = jitter
        self._usage = EmbeddingUsage()
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            follow_redirects=False,  # a redirect would carry the Authorization header elsewhere
            event_hooks={"request": [egress.request_hook("embeddings"), httpx_request_hook]},
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls,
        settings: EmbeddingSettings | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> EmbeddingClient:
        s = settings if settings is not None else EmbeddingSettings()
        return cls(
            spec_from_settings(s),
            base_url=s.AI_BASE_URL,
            api_key=s.AI_API_KEY.get_secret_value() if s.AI_API_KEY else None,
            timeout_seconds=s.AI_EMBEDDING_TIMEOUT_SECONDS,
            connect_timeout_seconds=s.AI_EMBEDDING_CONNECT_TIMEOUT_SECONDS,
            batch_size=s.INGEST_EMBED_BATCH_SIZE,
            max_attempts=s.AI_EMBEDDING_MAX_ATTEMPTS,
            allow_external=s.AI_EXTERNAL_PROVIDERS_ENABLED,
            sleeper=sleeper,
            transport=transport,
        )

    # ------------------------------------------------------------------ public API

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    @property
    def usage(self) -> EmbeddingUsage:
        """Cumulative usage of this client (only successful calls are counted)."""
        return self._usage

    def embed_documents_detailed(self, texts: list[str]) -> EmbeddingBatchResult:
        return self._embed(texts, self._spec.document_prefix)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents_detailed(texts).vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], self._spec.query_prefix).vectors[0]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EmbeddingClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"EmbeddingClient(model={self._spec.model_id!r}, space={self._spec.space_id})"

    # ------------------------------------------------------------------ internals

    def _embed(self, texts: list[str], prefix: str) -> EmbeddingBatchResult:
        if not texts:
            return EmbeddingBatchResult([], (), EmbeddingUsage())
        prepared: list[str] = []
        truncated: list[int] = []
        limit = self._spec.max_input_chars
        for i, text in enumerate(texts):
            if not isinstance(text, str):
                raise EmbeddingInputInvalid(f"input {i} is not a string")
            if not text.strip():
                raise EmbeddingInputInvalid(f"input {i} is blank")
            if len(text) > limit:
                text = text[:limit]
                truncated.append(i)
            prepared.append(prefix + text)

        vectors: list[list[float]] = []
        requests = 0
        for start in range(0, len(prepared), self._batch_size):
            batch = prepared[start : start + self._batch_size]
            body = self._post({"model": self._spec.model_id, "input": batch})
            vectors.extend(self._validate(body, len(batch)))
            requests += 1
        # Only reached when every batch validated: all-or-nothing.
        usage = EmbeddingUsage(
            requests=requests,
            inputs=len(prepared),
            estimated_tokens=sum(estimate_tokens(len(t)) for t in prepared),
            truncated_inputs=len(truncated),
        )
        self._usage = self._usage + usage
        return EmbeddingBatchResult(vectors, tuple(truncated), usage)

    def _backoff_delay(self, attempt: int) -> float:
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * float(2 ** (attempt - 1)))
        return float(ceiling * (0.5 + 0.5 * self._jitter()))  # 50-100 % of the exponential ceiling

    def _post(self, payload: dict[str, Any]) -> Any:
        reason = "unknown"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._http.post(self._url, json=payload)
            except httpx.TimeoutException:
                reason = "timeout"
            except httpx.TransportError as exc:
                reason = f"connection error ({type(exc).__name__})"
            else:
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        return response.json()
                    except ValueError:
                        raise EmbeddingResponseInvalid("response body is not valid JSON") from None
                if status != 429 and status < 500:
                    raise EmbeddingRequestRejected(status)
                reason = f"HTTP {status}"
            if attempt < self._max_attempts:
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "embedding request failed (%s); retry %d/%d in %.2fs",
                    reason,
                    attempt,
                    self._max_attempts - 1,
                    delay,
                )
                self._sleep(delay)
        raise EmbeddingUnavailable(reason, attempts=self._max_attempts)

    def _validate(self, body: Any, expected_count: int) -> list[list[float]]:
        spec = self._spec
        if not isinstance(body, dict):
            raise EmbeddingResponseInvalid("response is not a JSON object")
        echoed = body.get("model")
        if isinstance(echoed, str) and echoed != spec.model_id:
            raise EmbeddingModelMismatch(spec.model_id, echoed)
        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise EmbeddingResponseInvalid("response has no data")
        if len(data) != expected_count:
            raise EmbeddingResponseInvalid(
                f"response has {len(data)} vectors for {expected_count} inputs"
            )

        indexed: list[tuple[int, Any]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict) or "embedding" not in item:
                raise EmbeddingResponseInvalid("response item has no embedding")
            index = item.get("index", position)
            if isinstance(index, bool) or not isinstance(index, int):
                raise EmbeddingResponseInvalid("response item index is not an integer")
            indexed.append((index, item["embedding"]))
        indexed.sort(key=lambda pair: pair[0])
        if [i for i, _ in indexed] != list(range(expected_count)):
            raise EmbeddingResponseInvalid("response indexes are not 0..n-1 without gaps")

        result: list[list[float]] = []
        for index, raw in indexed:
            if not isinstance(raw, list):
                raise EmbeddingResponseInvalid("embedding is not a list")
            if len(raw) != spec.dimensions:
                raise EmbeddingDimensionMismatch(spec.dimensions, len(raw), index=index)
            vector: list[float] = []
            for value in raw:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise EmbeddingResponseInvalid("embedding contains a non-numeric value")
                number = float(value)
                if not math.isfinite(number):
                    raise EmbeddingResponseInvalid("embedding contains a non-finite value")
                vector.append(number)
            if spec.normalize:
                norm = math.sqrt(math.fsum(v * v for v in vector))
                if not math.isfinite(norm) or norm == 0.0:
                    raise EmbeddingResponseInvalid("embedding has zero or overflowing norm")
                vector = [v / norm for v in vector]
            result.append(vector)
        return result


def build_embedding_client(settings: EmbeddingSettings | None = None) -> EmbeddingClient:
    """The configured client (environment ``AI_*`` / ``INGEST_EMBED_BATCH_SIZE``).

    Raises ``EmbeddingConfigurationError`` (field names only) when the settings are invalid.
    """
    try:
        return EmbeddingClient.from_settings(settings)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
        raise EmbeddingConfigurationError(
            f"embedding settings invalid: {', '.join(fields)}"
        ) from None
