"""M05 chat hook: graph chunks reach the prompt (flag on); flag off never calls the graph."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.config import Settings
from app.modules.ai import chat
from app.modules.ai.models.fake import FakeChatModel
from app.modules.graph.fake_read import FakeChunkSource, FakeGraphRead
from tests.ai.test_chat_unit import FakeExecutor, make_executor
from tests.graph_retrieval.support import claim, db_for, ent, evidence_for, pub_rows, scope_of

QUESTION = "Which service is related to Atlas?"
TEXT = "Atlas and Garnet are both owned by the Core team."


def _graph_executor(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool, boom: bool = False
) -> tuple[FakeExecutor, FakeChatModel, FakeGraphRead, FakeChunkSource]:
    a, b = ent("Atlas"), ent("Garnet")
    c1 = claim(a, b, predicate="related_to")
    ev, chunk = evidence_for(c1)
    repo = FakeGraphRead([a, b], [c1], [ev])
    if boom:

        def fail(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("graph down")

        repo.search_entities = fail  # type: ignore[method-assign]
    chunks = FakeChunkSource({chunk: TEXT})
    model = FakeChatModel(["Atlas and Garnet are owned by the Core team [1]."])
    ex, _rec = make_executor(monkeypatch, model, evidence=[])
    ex.settings = Settings(GRAPH_ENABLED=enabled)
    ex.deps = dataclasses.replace(ex.deps, graph_repo=repo, graph_chunks=chunks)
    session = db_for(pub_rows([(ev, c1)]))

    @contextlib.contextmanager
    def graph_session() -> Iterator[Any]:
        yield session

    ex._session = graph_session  # type: ignore[method-assign]
    ex._load_input = lambda: chat.RunInput(  # type: ignore[method-assign]
        QUESTION, (), frozenset(), frozenset()
    )
    ex._resolve_scope = lambda _s: scope_of()  # type: ignore[method-assign,assignment]
    return ex, model, repo, chunks


def test_graph_chunk_reaches_the_prompt_and_is_cited(monkeypatch: pytest.MonkeyPatch) -> None:
    ex, model, repo, _chunks = _graph_executor(monkeypatch, enabled=True)
    assert ex.run() == "completed"
    prompt = " ".join(str(m) for m in model.calls[0].messages)
    assert TEXT in prompt
    assert "search_entities" in repo.calls


def test_flag_off_never_touches_the_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    ex, model, repo, chunks = _graph_executor(monkeypatch, enabled=False)
    assert ex.run() == "completed"  # no evidence -> abstains, no model call
    assert repo.calls == []
    assert chunks.calls == 0
    assert model.calls == []


def test_graph_failure_degrades_to_base_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    ex, model, _repo, _chunks = _graph_executor(monkeypatch, enabled=True, boom=True)
    assert ex.run() == "completed"
    assert model.calls == []  # base evidence was empty: abstention, not a failed run
