"""M13.01: GenAI spans via an in-memory exporter, no content in spans, off = no-op, metrics."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import tracing, tracing_genai

CANARY = "CANARY-secret question text"


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(tracing, "_otel_ready", True)
    monkeypatch.setattr(tracing_genai, "_tracer", lambda: provider.get_tracer("t"))
    return exp


def _value(name: str, labels: dict[str, str] | None = None) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_spans_attributes_nesting_and_no_content(exporter: InMemorySpanExporter) -> None:
    tracing_genai.set_prompt("v-1", "ab" * 32)
    tracing_genai.set_quota_state("ok")
    with tracing.bind(trace_id=tracing.new_trace_id(), span_id=tracing.new_span_id()):
        with tracing_genai.stage("retrieve", mode="hybrid", note=CANARY):
            tracing_genai.on_usage("chat", "openai", "m-1", 10, 20, 5, 120, 42)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    gen, ret = spans["ukos.generate"], spans["ukos.retrieve"]
    a = gen.attributes or {}
    assert a["gen_ai.system"] == "openai" and a["gen_ai.request.model"] == "m-1"
    assert a["gen_ai.usage.input_tokens"] == 10 and a["gen_ai.usage.output_tokens"] == 20
    assert a["ukos.cache.hit"] is True and a["ukos.cost.micros"] == 42
    assert a["ukos.prompt.version_id"] == "v-1" and a["ukos.quota.state"] == "ok"
    assert gen.parent is not None and gen.parent.span_id == ret.context.span_id
    assert (gen.end_time or 0) - (gen.start_time or 0) >= 100_000_000
    assert CANARY not in repr([(s.attributes, s.events, s.name) for s in spans.values()])


def test_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_otel_ready", False)

    def boom() -> Any:
        raise AssertionError("tracer touched while disabled")

    monkeypatch.setattr(tracing_genai, "_tracer", boom)
    with tracing_genai.stage("generate"):
        tracing_genai.on_usage("chat", "p", "m", 1, 1, 0, 1, 0)


def test_stage_propagates_errors(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError), tracing_genai.stage("verify"):
        raise ValueError("x")
    assert [s.name for s in exporter.get_finished_spans()] == ["ukos.verify"]


def test_metrics_increment_and_label_hygiene() -> None:
    before = _value("ukos_ai_tokens_total", {"kind": "graph", "direction": "input"})
    tracing_genai.on_usage("graph", "p", "m", 7, 3, 0, 50, 9)
    assert _value("ukos_ai_tokens_total", {"kind": "graph", "direction": "input"}) == before + 7
    c0 = _value("ukos_ai_cache_hits_total", {"cache": "result"})
    tracing_genai.record_cache_hit("result")
    assert _value("ukos_ai_cache_hits_total", {"cache": "result"}) == c0 + 1
    tracing_genai.record_provider_error("openai", "Bad Code with text " + CANARY)
    assert _value("ukos_provider_errors_total", {"provider": "openai", "code": "other"}) >= 1
    a0 = _value("ukos_ai_abstain_total")
    tracing_genai.record_abstain()
    assert _value("ukos_ai_abstain_total") == a0 + 1
