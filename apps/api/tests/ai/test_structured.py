"""Structured output + repair (M11.06)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.modules.ai.models.fake import FakeChatModel
from app.modules.ai.models.types import ChatMessage
from app.modules.ai.structured import ERR_NOT_JSON, ERR_SCHEMA, generate_structured
from app.modules.graph.extract import SourceChunk, extract_batch
from app.modules.research import planner


class Out(BaseModel):
    n: int


MSG: list[ChatMessage] = [{"role": "user", "content": "go"}]


def test_valid_and_fenced() -> None:
    r = generate_structured(FakeChatModel(['{"n": 1}']), MSG, Out)
    assert r.ok
    assert r.value == Out(n=1)
    assert not r.repaired
    fenced = generate_structured(FakeChatModel(['```json\n{"n": 2}\n```']), MSG, Out)
    assert fenced.value == Out(n=2)


def test_invalid_then_repaired_carries_error_not_prompt() -> None:
    m = FakeChatModel(['{"n": "x"}', '{"n": 3}'])
    r = generate_structured(m, MSG, Out)
    assert r.value == Out(n=3)
    assert r.repaired
    assert len(m.calls) == 2
    repair = m.calls[1].messages[-1]["content"]
    assert "n: " in repair
    assert "go" not in repair


def test_invalid_twice_is_safe_error() -> None:
    m = FakeChatModel(["secret prose", "still prose"])
    r = generate_structured(m, MSG, Out)
    assert r.value is None
    assert r.error is not None
    assert r.error.code == ERR_NOT_JSON
    assert "secret" not in r.error.detail
    r2 = generate_structured(FakeChatModel(['{"n": "a"}']), MSG, Out)
    assert r2.error is not None
    assert r2.error.code == ERR_SCHEMA
    assert len(FakeChatModel(["x"]).calls) == 0


def test_research_plan_malformed_and_repaired() -> None:
    for bad in ("", "no json", "[1, 2]", '{"subqueries": "a"}', '{"subqueries": []}'):
        assert planner.parse_plan(bad, 3) is None
    m = FakeChatModel(["nope", '{"subqueries": ["alpha beta"], "extra": 1}'])
    r = generate_structured(m, MSG, planner.PlanReply)
    assert r.value is not None
    assert planner.clean_plan(r.value, 3) == ["alpha beta"]


def _chunk() -> SourceChunk:
    u = uuid.uuid4
    return SourceChunk(u(), 0, "Alice Smith works for Acme Corp.", u(), u(), u(), u())


def test_graph_real_shaped_output_zero_based_chunk_and_extras() -> None:
    """Small local models answer chunk 0 for a single excerpt, wrap in fences and add prose."""
    real = (
        "Here is the graph:\n```json\n"
        '{"entities":[{"name":"Alice Smith","type":"person","chunk":0,"confidence":0.9},'
        '{"name":"Acme Corp","type":"organization","chunk":0}],'
        '"relations":[{"subject":"Alice Smith","predicate":"works_for","object":"Acme Corp",'
        '"chunk":0}],"notes":"x"}\n```\nHope this helps!'
    )
    out = extract_batch(FakeChatModel([real]), [_chunk()], max_entities_per_chunk=12)
    assert {e.name for e in out.entities.values()} == {"Alice Smith", "Acme Corp"}
    assert [r.predicate for r in out.relations] == ["works_for"]


def test_graph_malformed_repaired_then_empty() -> None:
    good = '{"entities":[{"name":"Acme Corp","type":"organization","chunk":1}],"relations":[]}'
    out = extract_batch(
        FakeChatModel(['{"entities": 5}', good]), [_chunk()], max_entities_per_chunk=5
    )
    assert len(out.entities) == 1
    bad = extract_batch(FakeChatModel(["garbage"]), [_chunk()], max_entities_per_chunk=5)
    assert bad.entities == {}
    assert bad.dropped == 1
