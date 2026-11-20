"""A local fake GitHub REST server (no real network): one repository, commits as snapshots.

Serves the five endpoints the connector uses: ``commits/{ref}``, ``git/trees/{sha}?recursive=1``,
``compare/{base}...{head}`` and ``git/blobs/{sha}``. ``commit({path: bytes | None})`` creates a new
HEAD (``None`` deletes the path). ``fail_next``/``rate_limit`` simulate upstream trouble, and every
request's ``Authorization`` header is recorded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()  # noqa: S324


class FakeGitHub:
    def __init__(self, repo: str = "acme/docs") -> None:
        self.repo = repo
        self.snapshots: dict[str, dict[str, bytes]] = {}
        self.order: list[str] = []
        self.blobs: dict[str, bytes] = {}
        self.auth_seen: list[str | None] = []
        self.requests: list[str] = []
        self.fail_status: int | None = None  # every request answers this status while set
        self.rate_limited = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def head(self) -> str:
        return self.order[-1]

    def commit(self, changes: dict[str, bytes | None]) -> str:
        files = dict(self.snapshots[self.order[-1]]) if self.order else {}
        for path, data in changes.items():
            if data is None:
                files.pop(path, None)
            else:
                files[path] = data
                self.blobs[_blob_sha(data)] = data
        sha = hashlib.sha1(f"commit-{len(self.order)}-{sorted(files)}".encode()).hexdigest()  # noqa: S324
        self.snapshots[sha] = files
        self.order.append(sha)
        return sha

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- http ------------------------------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                fake.auth_seen.append(self.headers.get("Authorization"))
                url = urlparse(self.path)
                fake.requests.append(url.path)
                if fake.rate_limited:
                    self._send(403, {"message": "rate limit"}, {"x-ratelimit-remaining": "0"})
                    return
                if fake.fail_status:
                    self._send(fake.fail_status, {"message": "boom"})
                    return
                base = f"/repos/{fake.repo}/"
                if not url.path.startswith(base):
                    self._send(404, {"message": "Not Found"})
                    return
                rest = url.path[len(base) :]
                if rest.startswith("commits/"):
                    self._send(200, {"sha": fake.head})
                elif rest.startswith("git/trees/"):
                    files = fake.snapshots.get(rest.split("/")[-1])
                    if files is None:
                        self._send(404, {"message": "Not Found"})
                        return
                    tree = [
                        {"type": "blob", "path": p, "sha": _blob_sha(d), "size": len(d)}
                        for p, d in sorted(files.items())
                    ]
                    self._send(200, {"truncated": False, "tree": tree})
                elif rest.startswith("compare/"):
                    self._compare(rest[len("compare/") :], parse_qs(url.query))
                elif rest.startswith("git/blobs/"):
                    data = fake.blobs.get(rest.split("/")[-1])
                    if data is None:
                        self._send(404, {"message": "Not Found"})
                        return
                    self._send(
                        200,
                        {
                            "size": len(data),
                            "encoding": "base64",
                            "content": base64.b64encode(data).decode(),
                        },
                    )
                else:
                    self._send(404, {"message": "Not Found"})

            def _compare(self, spec: str, _query: dict[str, list[str]]) -> None:
                base_sha, _, head_sha = spec.partition("...")
                old, new = fake.snapshots.get(base_sha), fake.snapshots.get(head_sha)
                if old is None or new is None:
                    self._send(404, {"message": "No common ancestor"})
                    return
                files: list[dict[str, str]] = []
                for path, data in sorted(new.items()):
                    if path not in old:
                        files.append({"filename": path, "status": "added", "sha": _blob_sha(data)})
                    elif old[path] != data:
                        files.append(
                            {"filename": path, "status": "modified", "sha": _blob_sha(data)}
                        )
                files += [
                    {"filename": p, "status": "removed", "sha": _blob_sha(d)}
                    for p, d in sorted(old.items())
                    if p not in new
                ]
                self._send(200, {"status": "ahead", "files": files})

        return Handler
