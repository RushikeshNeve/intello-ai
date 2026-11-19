"""Helpers for the gateway tests: evidence objects, SSE bodies, an in-process SSE server."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID, uuid4

from app.modules.ai.models.settings import ChatSettings


@dataclass(frozen=True)
class Ev:
    """Satisfies ``EvidenceLike`` (the retrieval agent's ``Evidence`` does too)."""

    evidence_id: str
    text: str
    title: str = "Doc"
    score: float = 1.0
    page_start: int | None = 1
    page_end: int | None = 1
    heading_path: tuple[str, ...] = ()
    chunk_id: UUID = field(default_factory=uuid4)
    document_id: UUID = field(default_factory=uuid4)
    version_id: UUID = field(default_factory=uuid4)


def drain(it: Iterator[str], sink: list[str]) -> None:
    """Consume ``it`` into ``sink`` (keeps ``pytest.raises`` blocks to one statement)."""
    for item in it:
        sink.append(item)


def evs(*texts: str) -> list[Ev]:
    return [Ev(f"e{i + 1}", t, score=1.0 - i * 0.01) for i, t in enumerate(texts)]


def settings(**env: object) -> ChatSettings:
    return ChatSettings(**env)  # type: ignore[arg-type]


def sse_event(obj: object) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def delta_event(text: str) -> bytes:
    return sse_event({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})


def finish_event(reason: str = "stop") -> bytes:
    return sse_event({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})


DONE = b"data: [DONE]\n\n"


def sse_body(*deltas: str, done: bool = True, finish: bool = True) -> bytes:
    body = b"".join(delta_event(d) for d in deltas)
    if finish:
        body += finish_event()
    if done:
        body += DONE
    return body


class SseServer:
    """A real HTTP server answering ``POST /v1/chat/completions`` with scripted raw byte chunks."""

    def __init__(self, chunks: Sequence[bytes], *, status: int = 200, pause: float = 0.0) -> None:
        self.chunks = list(chunks)
        self.status = status
        self.pause = pause
        self.requests: list[dict[str, object]] = []
        self.authorization: list[str | None] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                owner.requests.append(json.loads(self.rfile.read(length)))
                owner.authorization.append(self.headers.get("Authorization"))
                self.send_response(owner.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for chunk in owner.chunks:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if owner.pause:
                            time.sleep(owner.pause)
                except OSError:
                    pass
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> SseServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def sse_server(chunks: Sequence[bytes], **kw: float) -> Iterator[SseServer]:
    with SseServer(chunks, **kw) as s:  # type: ignore[arg-type]
        yield s
