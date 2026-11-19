"""Helpers of the M04 chat runtime tests: a recording index, an SSE client, chat API calls, the
worker/dispatcher environment and observations of runs, events and messages."""

from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import redis
from compose_support import ComposeProject, wait_until
from runtime_support import runtime_env
from sqlalchemy import Engine, text
from tenancy_support import Api, Response, User

REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

# Test knowledge: distinctive, easily supported sentences.
HERON = "Herons are wading birds that feed on fish and frogs in shallow water."
HERON_ANSWER = "Herons feed on fish and frogs in shallow water [1]."
HERON_Q = "What do herons feed on?"
OTTER = "Otters build their dens near rivers and eat crayfish at dusk."


# -- the index the worker process can see -------------------------------------------------------


class RecordingRepo:
    """Wraps an in-memory repository and appends every write to ``corpus.jsonl`` so the worker
    process (``chat_fake_runtime.FileBackedRepo``) can rebuild the same index."""

    def __init__(self, inner: Any, path: Path) -> None:
        self._inner = inner
        self._path = path

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _append(self, op: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(op, default=str) + "\n")

    def ensure_indexes(self, scope: Any, space: str, dims: int) -> None:
        self._inner.ensure_indexes(scope, space, dims)
        self._append({"op": "indexes", "org": str(scope.org_id), "space": space, "dims": dims})

    def upsert_chunks(self, scope: Any, records: list[Any], embeddings: Any) -> Any:
        result = self._inner.upsert_chunks(scope, records, embeddings)
        self._append(
            {
                "op": "upsert",
                "org": str(scope.org_id),
                "records": [dataclasses.asdict(r) for r in records],
            }
        )
        return result

    def set_published(self, scope: Any, revision_id: uuid.UUID, flag: bool) -> Any:
        result = self._inner.set_published(scope, revision_id, flag)
        self._append(
            {"op": "publish", "org": str(scope.org_id), "revision": str(revision_id), "flag": flag}
        )
        return result


# -- SSE client ------------------------------------------------------------------------------------


@dataclass
class Frame:
    id: int | None = None
    event: str | None = None
    data: Any = None
    comment: str | None = None
    retry: int | None = None


def parse_frames(lines: Any) -> Any:
    cur = Frame()
    has = False
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if has:
                yield cur
            cur, has = Frame(), False
            continue
        has = True
        if line.startswith(":"):
            cur.comment = line[1:].strip()
        elif line.startswith("retry:"):
            cur.retry = int(line.split(":", 1)[1])
        elif line.startswith("id:"):
            cur.id = int(line.split(":", 1)[1])
        elif line.startswith("event:"):
            cur.event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            cur.data = json.loads(line.split(":", 1)[1])


def sse_url(base: str, org: uuid.UUID, run: uuid.UUID | str) -> str:
    return f"{base}/v1/orgs/{org}/chat/runs/{run}/events"


def read_sse(
    base: str,
    org: uuid.UUID,
    run: uuid.UUID | str,
    user: User | None,
    *,
    last_event_id: int | None = None,
    stop_after: int | None = None,
    until: Callable[[Frame], bool] | None = None,
    timeout: float = 90.0,
    query_last_id: bool = False,
) -> tuple[int, dict[str, str], list[Frame]]:
    """Read the stream until it ends, ``stop_after`` data frames were read (the connection is then
    closed from the client side: a killed tab) or ``until(frame)``. Returns (status, headers,
    frames); a non-200 answer returns without frames."""
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if user is not None:
        headers["Authorization"] = f"Bearer {user.token}"
    if last_event_id is not None:
        if query_last_id:
            params["last_event_id"] = str(last_event_id)
        else:
            headers["Last-Event-ID"] = str(last_event_id)
    frames: list[Frame] = []
    deadline = time.monotonic() + timeout
    with httpx.stream(
        "GET",
        sse_url(base, org, run),
        headers=headers,
        params=params,
        timeout=httpx.Timeout(timeout, connect=10.0),
    ) as response:
        if response.status_code != 200:
            response.read()
            return response.status_code, dict(response.headers), frames
        data_frames = 0
        for frame in parse_frames(response.iter_lines()):
            frames.append(frame)
            if frame.event is not None and frame.event != "end":
                data_frames += 1
            if frame.event == "end":
                break
            if stop_after is not None and data_frames >= stop_after:
                break
            if until is not None and until(frame):
                break
            if time.monotonic() > deadline:
                raise AssertionError("SSE read timed out")
        return response.status_code, dict(response.headers), frames


def data_frames(frames: list[Frame]) -> list[Frame]:
    return [f for f in frames if f.event is not None and f.event != "end"]


def end_reason(frames: list[Frame]) -> str | None:
    ends = [f for f in frames if f.event == "end"]
    return ends[-1].data["reason"] if ends else None


def streamed_text(frames: list[Frame]) -> str:
    return "".join(f.data["text"] for f in frames if f.event == "delta")


# -- chat API calls ------------------------------------------------------------------------


def base_path(org: uuid.UUID) -> str:
    return f"/v1/orgs/{org}/chat"


def new_thread(api: Api, org: uuid.UUID, user: User, **body: Any) -> uuid.UUID:
    res = api.post(f"{base_path(org)}/threads", user, body)
    assert res.status == 201, res.body
    return uuid.UUID(res.body["id"])


def ask(
    api: Api,
    org: uuid.UUID,
    thread: uuid.UUID,
    user: User,
    content: str,
    *,
    key: str | None = None,
    **extra: Any,
) -> Response:
    body = {"content": content, "idempotency_key": key or f"key-{uuid.uuid4().hex}", **extra}
    return api.post(f"{base_path(org)}/threads/{thread}/runs", user, body)


def ask_ok(
    api: Api, org: uuid.UUID, thread: uuid.UUID, user: User, content: str, **kw: Any
) -> uuid.UUID:
    res = ask(api, org, thread, user, content, **kw)
    assert res.status in (200, 202), res.body
    return uuid.UUID(res.body["run_id"])


# -- observations (admin role: bypasses RLS) -----------------------------------------------------


def run_row(admin: Engine, run_id: uuid.UUID) -> dict[str, Any]:
    with admin.connect() as conn:
        return dict(
            conn.execute(text("SELECT * FROM chat_runs WHERE id = :r"), {"r": run_id})
            .mappings()
            .one()
        )


def run_events(admin: Engine, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with admin.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT seq, type, data FROM chat_run_events WHERE run_id = :r ORDER BY seq"),
                {"r": run_id},
            ).mappings()
        ]


def event_types(admin: Engine, run_id: uuid.UUID) -> list[str]:
    return [e["type"] for e in run_events(admin, run_id)]


def messages(admin: Engine, thread: uuid.UUID) -> list[dict[str, Any]]:
    with admin.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT * FROM chat_messages WHERE thread_id = :t ORDER BY created_at, id"),
                {"t": thread},
            ).mappings()
        ]


def wait_run(
    admin: Engine, run_id: uuid.UUID, states: set[str], timeout: float = 60.0
) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        row = run_row(admin, run_id)
        return row if row["state"] in states else None

    return wait_until(probe, timeout=timeout, interval=0.2, description=f"run {run_id} in {states}")


TERMINAL = {"completed", "failed", "cancelled"}


def scenario(directory: Path, name: str, **script: Any) -> str:
    """Write ``scn_<name>.json``; returns the token to put into the question."""
    (directory / f"scn_{name}.json").write_text(json.dumps(script), encoding="utf-8")
    return f"SCN:{name}"


def model_calls(directory: Path, name: str | None = None) -> int:
    path = directory / "calls.log"
    if not path.exists():
        return 0
    lines = [ln.split()[0] for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    return len(lines) if name is None else lines.count(name)


# -- processes -----------------------------------------------------------------------------------


def chat_env(stack: ComposeProject, fake_dir: Path, **overrides: str) -> dict[str, str]:
    dirs = [
        REPO_ROOT / "apps" / "api",
        HERE,
        REPO_ROOT / "tests" / "integration" / "m03_runtime",
        REPO_ROOT / "tests" / "integration" / "m04_retrieval",
        REPO_ROOT / "tests" / "integration" / "m02_catalog",
        REPO_ROOT / "tests" / "integration" / "m01_tenancy",
        REPO_ROOT / "tests" / "integration" / "m01_foundation",
        REPO_ROOT / "tests" / "integration" / "m02_foundation",
        REPO_ROOT / "tests" / "infrastructure",
    ]
    env = runtime_env(
        stack,
        fake_dir,
        FAKE_CHAT_DIR=str(fake_dir),
        CHAT_RUNTIME_FACTORY="chat_fake_runtime:build_deps",
        CHAT_LEASE_SECONDS="6",
        CHAT_HEARTBEAT_SECONDS="2",
        AI_BASE_URL="http://127.0.0.1:9/v1",
        PYTHONPATH=os.pathsep.join(str(d) for d in dirs),
    )
    env.update(overrides)
    return env


def drain_chat_backlog(admin_engine: Engine, redis_client: redis.Redis) -> None:
    """Runs left queued by earlier tests must not be executed once a dispatcher exists."""
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET dispatched_at = now() WHERE dispatched_at IS NULL"
                " AND event_type = 'chat.run.queued'"
            )
        )
        conn.execute(
            text(
                "UPDATE chat_runs SET state = 'cancelled', finished_at = now(),"
                " lease_owner = NULL, lease_expires_at = NULL"
                " WHERE state IN ('queued', 'running')"
            )
        )
    redis_client.flushdb()
