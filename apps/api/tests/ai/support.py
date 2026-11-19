"""Test helpers: an in-process OpenAI-compatible stub server and MockTransport response builders."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from app.modules.ai.embeddings.fake import hash_embed
from app.modules.ai.embeddings.registry import DEFAULT_SPEC


def embedding_response(
    inputs: list[str],
    *,
    dimensions: int = DEFAULT_SPEC.dimensions,
    model: str | None = DEFAULT_SPEC.model_id,
    reverse: bool = False,
) -> dict[str, Any]:
    """An OpenAI-shaped body whose vectors are ``hash_embed`` of the inputs the server received."""
    data = [
        {"object": "embedding", "index": i, "embedding": hash_embed(t, dimensions)}
        for i, t in enumerate(inputs)
    ]
    if reverse:
        data.reverse()
    body: dict[str, Any] = {"object": "list", "data": data, "usage": {"prompt_tokens": 0}}
    if model is not None:
        body["model"] = model
    return body


class Recorder:
    """A MockTransport handler that records requests and answers from a callable."""

    def __init__(self, respond: Callable[[httpx.Request, dict[str, Any]], httpx.Response]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = json.loads(request.content) if request.content else {}
        self.payloads.append(payload)
        return self.respond(request, payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def sent_inputs(self) -> list[list[str]]:
        return [p["input"] for p in self.payloads]


def ok(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=embedding_response(payload["input"]))


class StubServer:
    """A real HTTP server on a free local port with the node stub's endpoints and fault knobs."""

    def __init__(self) -> None:
        self.fail_next = 0
        self.fail_status = 503
        self.delay_seconds = 0.0
        self.wrong_dimensions = False
        self.wrong_count = False
        self.reverse_order = False
        self.echo_model: str | None = None
        self.requests = 0
        self.last_authorization: str | None = None
        self.input_batches: list[int] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": DEFAULT_SPEC.model_id}]})
                else:
                    self._send(404, {})

            def do_POST(self) -> None:
                if self.path != "/v1/embeddings":
                    self._send(404, {})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                owner.requests += 1
                owner.last_authorization = self.headers.get("Authorization")
                if owner.delay_seconds:
                    time.sleep(owner.delay_seconds)
                if owner.fail_next > 0:
                    owner.fail_next -= 1
                    self._send(owner.fail_status, {"error": {"message": "injected"}})
                    return
                inputs = payload["input"]
                owner.input_batches.append(len(inputs))
                dims = DEFAULT_SPEC.dimensions - (1 if owner.wrong_dimensions else 0)
                body = embedding_response(
                    inputs,
                    dimensions=dims,
                    model=owner.echo_model or payload.get("model"),
                    reverse=owner.reverse_order,
                )
                if owner.wrong_count:
                    body["data"] = body["data"][:-1]
                self._send(200, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def start(self) -> StubServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextmanager
def running_stub() -> Iterator[StubServer]:
    stub = StubServer().start()
    try:
        yield stub
    finally:
        stub.stop()
