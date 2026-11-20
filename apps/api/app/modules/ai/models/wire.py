"""OpenAI-compatible ``POST {base}/chat/completions`` client with a streaming SSE parser.

Shared by ``LocalChatModel`` (LM Studio, llama.cpp, Ollama, vLLM) and ``OpenAIChatModel``.

* Retries (bounded, exponential backoff with jitter) only BEFORE the first token was yielded:
  timeouts, connection errors, 429, 5xx and a stream that dies before producing text. After the
  first token a failure raises ``ChatStreamInterrupted`` (retrying would duplicate text).
* SSE: comment/keepalive lines (``:``), ``event:``/``id:``/``retry:`` fields, multi-line ``data:``,
  CR/LF/CRLF line ends (also split across chunks), partial UTF-8 sequences across chunks,
  ``data: [DONE]``, mid-stream ``{"error": ...}`` objects. ``reasoning_content`` is ignored.
* Safety: errors and logs never contain prompt/answer text, the key, a response body or a URL.
  Redirects are not followed (the Authorization header would travel).
"""

from __future__ import annotations

import codecs
import json
import logging
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from types import TracebackType
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit

import httpx

from app.core.tracing import httpx_request_hook
from app.modules.ai.embeddings.client import is_local_host
from app.modules.ai.models.errors import (
    ChatConfigurationError,
    ChatModelUnavailable,
    ChatRequestRejected,
    ChatResponseInvalid,
    ChatStreamInterrupted,
)
from app.modules.ai.models.types import ChatMessage, ModelSpec

logger = logging.getLogger("ukos.ai.chat")

BACKOFF_BASE_SECONDS: Final = 0.5
BACKOFF_CAP_SECONDS: Final = 8.0
MAX_LINE_CHARS: Final = 1_048_576
DEFAULT_MAX_RESPONSE_CHARS: Final = 200_000

_LINE_END = re.compile(r"\r\n|\r|\n")


def validate_base_url(base_url: str, *, allow_external: bool) -> str:
    parts = urlsplit(base_url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ChatConfigurationError("base URL must be an http(s) URL with a host")
    if parts.username or parts.password:
        raise ChatConfigurationError("base URL must not embed credentials")
    if parts.query or parts.fragment:
        raise ChatConfigurationError("base URL must not have a query or fragment")
    if not allow_external and not is_local_host(parts.hostname):
        raise ChatConfigurationError("base URL is not a local host and external use is not allowed")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def iter_sse_data(
    chunks: Iterable[bytes], *, deadline_check: Callable[[], None] | None = None
) -> Iterator[str]:
    """Yield the ``data`` payload of each SSE event (comments and other fields dropped)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    buf = ""
    data_lines: list[str] = []
    first = True

    def handle(line: str) -> str | None:
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines.clear()
                return payload
            return None
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if name == "data":
            data_lines.append(value[1:] if sep and value.startswith(" ") else value)
        return None

    try:
        for chunk in chunks:
            if deadline_check is not None:
                deadline_check()
            buf += decoder.decode(chunk)
            if first and buf:
                buf = buf.lstrip("﻿")
                first = bool(not buf)
            pos = 0
            for match in _LINE_END.finditer(buf):
                if match.group() == "\r" and match.end() == len(buf):
                    break  # may be the first half of CRLF: wait for the next chunk
                out = handle(buf[pos : match.start()])
                pos = match.end()
                if out is not None:
                    yield out
            buf = buf[pos:]
            if len(buf) > MAX_LINE_CHARS:
                raise ChatResponseInvalid("stream line too long")
        buf += decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise ChatResponseInvalid("stream is not valid UTF-8") from None
    for line in _LINE_END.split(buf):
        out = handle(line)
        if out is not None:
            yield out
    out = handle("")
    if out is not None:
        yield out


class OpenAICompatibleChat:
    provider: ClassVar[str] = "local"
    max_tokens_field: ClassVar[str] = "max_tokens"

    def __init__(
        self,
        spec: ModelSpec,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        connect_timeout_seconds: float = 5.0,
        generation_timeout_seconds: float = 120.0,
        max_attempts: int = 3,
        allow_external: bool = False,
        max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ChatConfigurationError("max_attempts must be >= 1")
        self.spec = spec
        self._url = f"{validate_base_url(base_url, allow_external=allow_external)}/chat/completions"
        self._max_attempts = max_attempts
        self._generation_timeout = generation_timeout_seconds
        self._max_response_chars = max_response_chars
        self._sleep = sleeper
        self._jitter = jitter
        self._clock = clock
        self.last_finish_reason: str | None = None
        headers = {"Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [httpx_request_hook]},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OpenAICompatibleChat:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.spec.model_id!r})"

    # ------------------------------------------------------------------ public API

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self.stream(messages, temperature=temperature, max_tokens=max_tokens))

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        payload: dict[str, Any] = {
            "model": self.spec.model_id,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "stream": True,
            "temperature": temperature,
            self.max_tokens_field: max_tokens
            if max_tokens is not None
            else self.spec.max_output_tokens,
        }
        if stop:
            payload["stop"] = list(stop)
        self.last_finish_reason = None
        started = self._clock()
        produced = False
        total_chars = 0
        reason = "unknown"

        def check_deadline() -> None:
            if self._clock() - started > self._generation_timeout:
                raise ChatStreamInterrupted("generation timeout")

        for attempt in range(1, self._max_attempts + 1):
            try:
                with self._http.stream("POST", self._url, json=payload) as response:
                    status = response.status_code
                    if status != 200:
                        if status == 429 or status >= 500:
                            reason = f"HTTP {status}"
                        else:
                            raise ChatRequestRejected(status)
                    else:
                        for delta in self._deltas(response.iter_bytes(), check_deadline):
                            produced = True
                            total_chars += len(delta)
                            if total_chars > self._max_response_chars:
                                raise ChatResponseInvalid("response too long")
                            yield delta
                        return
            except httpx.TimeoutException:
                reason = "timeout"
            except httpx.TransportError as exc:
                reason = f"connection error ({type(exc).__name__})"
            except ChatStreamInterrupted as exc:
                if produced or exc.reason == "generation timeout":
                    raise
                reason = exc.reason
            if produced:
                raise ChatStreamInterrupted(reason)
            if attempt < self._max_attempts:
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "chat request failed (%s); retry %d/%d in %.2fs",
                    reason,
                    attempt,
                    self._max_attempts - 1,
                    delay,
                )
                self._sleep(delay)
        raise ChatModelUnavailable(reason, attempts=self._max_attempts)

    # ------------------------------------------------------------------ internals

    def _backoff_delay(self, attempt: int) -> float:
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * float(2 ** (attempt - 1)))
        return float(ceiling * (0.5 + 0.5 * self._jitter()))

    def _deltas(self, chunks: Iterable[bytes], check_deadline: Callable[[], None]) -> Iterator[str]:
        finished = False
        for data in iter_sse_data(chunks, deadline_check=check_deadline):
            if data.strip() == "[DONE]":
                finished = True
                break
            try:
                obj = json.loads(data)
            except ValueError:
                raise ChatResponseInvalid("stream event is not valid JSON") from None
            if not isinstance(obj, dict):
                raise ChatResponseInvalid("stream event is not an object")
            if obj.get("error") is not None:
                raise ChatStreamInterrupted("server reported an error")
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                continue  # e.g. a trailing usage-only chunk
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ChatResponseInvalid("stream choice is not an object")
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                finished = True
                self.last_finish_reason = reason
        if not finished:
            raise ChatStreamInterrupted("stream ended before completion")
