"""Worker-side fakes of the chat runtime tests, resolved by the REAL Celery worker process through
``CHAT_RUNTIME_FACTORY=chat_fake_runtime:build_deps``.

* The knowledge index is an in-memory repository rebuilt from ``<FAKE_CHAT_DIR>/corpus.jsonl``
  (the tests append to it through ``chat_support.RecordingRepo`` while they publish documents), so
  the worker process sees exactly what the test published, without RushDB.
* The chat model is a script chosen by the question: a ``SCN:<name>`` token selects
  ``<FAKE_CHAT_DIR>/scn_<name>.json`` (see ``ScriptedModel``). The gateway's REAL ``get_chat_model``
  still runs first, so the organization policy is enforced exactly as in production; only the
  returned model is replaced. Every model call appends a line to ``calls.log``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from threading import Lock
from typing import Any

from ret_support import SPEC
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.modules.ai.chat import ChatDeps
from app.modules.ai.embeddings import FakeEmbeddingClient
from app.modules.ai.gateway import get_chat_model
from app.modules.ai.models.errors import ChatModelUnavailable, ChatStreamInterrupted
from app.modules.ai.models.policy import PolicySource
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import ChatMessage, ModelSpec
from app.modules.knowledge.ports import ChunkRecord, validated_org_scope
from app.workers.worker_db import get_worker_engine, worker_session

_UUID_FIELDS = ("org_id", "workspace_id", "document_id", "version_id", "revision_id", "chunk_id")
_SCN = re.compile(r"SCN:([A-Za-z0-9_]+)")


class FileBackedRepo:
    """Delegates to an in-memory repository rebuilt whenever the corpus file changes."""

    def __init__(self, path: Path, settings: Settings) -> None:
        self._path = path
        self._settings = settings
        self._lock = Lock()
        self._stamp: tuple[int, int] | None = None
        self._inner = InMemoryKnowledgeRepository()
        self._embedder = FakeEmbeddingClient(SPEC)

    def _current(self) -> InMemoryKnowledgeRepository:
        with self._lock:
            try:
                st = self._path.stat()
            except FileNotFoundError:
                return self._inner
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                self._inner = self._rebuild()
                self._stamp = stamp
            return self._inner

    def _rebuild(self) -> InMemoryKnowledgeRepository:
        repo = InMemoryKnowledgeRepository()
        engine = get_worker_engine(self._settings)
        scopes: dict[uuid.UUID, Any] = {}

        def scope_of(org_id: uuid.UUID) -> Any:
            if org_id not in scopes:
                session: Session
                with worker_session(engine, org_id) as session:
                    scopes[org_id] = validated_org_scope(session, org_id)
            return scopes[org_id]

        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            op = json.loads(line)
            scope = scope_of(uuid.UUID(op["org"]))
            if op["op"] == "indexes":
                repo.ensure_indexes(scope, op["space"], int(op["dims"]))
            elif op["op"] == "upsert":
                records = []
                for raw in op["records"]:
                    fields = dict(raw)
                    for name in _UUID_FIELDS:
                        fields[name] = uuid.UUID(fields[name])
                    records.append(ChunkRecord(**fields))
                repo.upsert_chunks(
                    scope, records, self._embedder.embed_documents([r.text for r in records])
                )
            elif op["op"] == "publish":
                repo.set_published(scope, uuid.UUID(op["revision"]), bool(op["flag"]))
        return repo

    def __getattr__(self, name: str) -> Any:
        return getattr(self._current(), name)


class ScriptedModel:
    """``scn_<name>.json``: ``{"text": str, "chunk": 6, "delay": 0.0, "error_after": null,
    "error_at_end": false}``. ``error_after=n`` raises a transient model error before piece ``n``
    (0 = before the first token); ``error_at_end`` interrupts the stream after the last piece."""

    def __init__(self, spec: ModelSpec, directory: Path) -> None:
        self.spec = spec
        self._dir = directory

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        question = messages[-1]["content"]
        found = _SCN.search(question)
        name = found.group(1) if found else "default"
        with (self._dir / "calls.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{name} {os.getpid()}\n")
        path = self._dir / f"scn_{name}.json"
        script: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"text": "ok"}
        )
        text: str = script.get("text", "")
        size = int(script.get("chunk", 6))
        pieces = [text[i : i + size] for i in range(0, len(text), size)] or [""]
        for index, piece in enumerate(pieces):
            if script.get("error_after") is not None and index == int(script["error_after"]):
                raise ChatModelUnavailable("scripted")
            if script.get("delay"):
                time.sleep(float(script["delay"]))
            yield piece
        if script.get("error_at_end"):
            raise ChatStreamInterrupted("scripted")

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self.stream(messages, temperature=temperature, max_tokens=max_tokens))


def model_factory(
    policy_source: PolicySource, org_id: uuid.UUID, cs: ChatSettings
) -> ScriptedModel:
    real = get_chat_model(policy_source, org_id, cs)  # the real policy choke point (may deny)
    return ScriptedModel(real.spec, Path(os.environ["FAKE_CHAT_DIR"]))


def build_deps(settings: Settings) -> ChatDeps:
    directory = Path(os.environ["FAKE_CHAT_DIR"])
    return ChatDeps(
        repo=FileBackedRepo(directory / "corpus.jsonl", settings),  # type: ignore[arg-type]
        embedder=FakeEmbeddingClient(SPEC),
        chat_settings=ChatSettings(),
        model_factory=model_factory,
    )
