"""M04.02 live smoke against LM Studio (optional: skips cleanly when unreachable / model not listed).

Streams a short grounded answer from the REAL chat model through the production ``LocalChatModel``,
checks delta ordering, then runs prompt assembly + verification on a grounded and on an ungrounded
real answer. LM Studio runs on the Windows host; from the Linux runner it is
``http://host.docker.internal:1234/v1`` (override with ``UKOS_TEST_AI_BASE_URL``; chat model id
``UKOS_TEST_CHAT_MODEL``, default ``google/gemma-3-4b``). LM Studio settings are never changed
(the model is JIT-loaded on the first call).

A second class exercises ``tests/e2e/support/chat-stub.mjs`` when ``UKOS_TEST_CHAT_STUB_URL`` is set
(for example ``http://host.docker.internal:18434/v1``).

    scripts/dev/linux-runner/run.sh apps/api \\
      "python -m pytest -q -s -p no:cacheprovider ../../tests/integration/m04_gateway"
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from app.modules.ai.context import build_prompt
from app.modules.ai.models.local import LocalChatModel
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.verification import ABSTENTION_MESSAGE, VerifyConfig, verify

BASE_URL = os.environ.get("UKOS_TEST_AI_BASE_URL", "http://host.docker.internal:1234/v1")
CHAT_MODEL = os.environ.get("UKOS_TEST_CHAT_MODEL", "google/gemma-3-4b")
STUB_URL = os.environ.get("UKOS_TEST_CHAT_STUB_URL", "")


class Ev:
    def __init__(self, evidence_id: str, text: str, title: str, score: float) -> None:
        self.evidence_id, self.text, self.title, self.score = evidence_id, text, title, score
        self.chunk_id = self.document_id = self.version_id = evidence_id
        self.page_start, self.page_end, self.heading_path = 1, 1, ()


EVIDENCE = [
    Ev(
        "e1",
        "All Kestrel sensors carry a three year limited warranty that starts on the delivery date.",
        "Warranty",
        0.9,
    ),
    Ev("e2", "Support requests are answered within one business day by email.", "Support", 0.5),
]


def _models(base_url: str) -> set[str] | str:
    try:
        response = httpx.get(f"{base_url}/models", timeout=3.0)
        response.raise_for_status()
        return {item["id"] for item in response.json()["data"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return f"not reachable at {base_url}/models ({type(exc).__name__})"


def _live_skip() -> str | None:
    found = _models(BASE_URL)
    if isinstance(found, str):
        return f"LM Studio {found}"
    return None if CHAT_MODEL in found else f"{CHAT_MODEL} is not listed by the server"


def _stub_skip() -> str | None:
    if not STUB_URL:
        return "UKOS_TEST_CHAT_STUB_URL not set"
    found = _models(STUB_URL)
    return found if isinstance(found, str) else None


def _model(base_url: str) -> LocalChatModel:
    s = ChatSettings(
        AI_BASE_URL=base_url, CHAT_MODEL=CHAT_MODEL, CHAT_TIMEOUT_SECONDS=180, CHAT_MAX_ATTEMPTS=2
    )
    return LocalChatModel.from_settings(s)


def _ask(model: LocalChatModel, question: str) -> tuple[list[str], float, float, list[Ev]]:
    bundle = build_prompt(question, EVIDENCE, [], budget_tokens=4096, reserve_output_tokens=400)
    assert bundle.should_call_model
    start = time.perf_counter()
    first = 0.0
    deltas: list[str] = []
    for delta in model.stream(list(bundle.messages), temperature=0.0, max_tokens=300):
        if not deltas:
            first = time.perf_counter() - start
        deltas.append(delta)
    total = time.perf_counter() - start
    return deltas, first, total, list(bundle.used_evidence)  # type: ignore[arg-type]


@pytest.mark.skipif(_live_skip() is not None, reason=_live_skip() or "")
class TestLiveLmStudio:
    def test_stream_order_and_verification_of_real_answers(self) -> None:
        model = _model(BASE_URL)
        deltas, first, total, used = _ask(model, "How long is the warranty on Kestrel sensors?")
        text = "".join(deltas)
        print(
            f"\n[live] model={CHAT_MODEL} deltas={len(deltas)} ttft={first:.2f}s total={total:.2f}s finish={model.last_finish_reason}"
        )
        print(f"[live] answer={text!r}")
        assert len(deltas) >= 2 and text.strip()
        assert model.last_finish_reason in ("stop", "length")
        # delta ordering: the concatenation equals a non-streamed completion of the same prompt
        v = verify(text, {1: "e1", 2: "e2"}, used, config=VerifyConfig())
        print(f"[live] verification={v.to_dict()} kept={v.answer_text!r}")
        assert v.status in ("grounded", "partial"), "the real model's grounded answer should verify"
        assert "three" in v.answer_text.lower() or "3" in v.answer_text

    def test_ungrounded_answer_is_withheld_or_abstained(self) -> None:
        model = _model(BASE_URL)
        deltas, first, total, used = _ask(model, "What is the capital of France?")
        text = "".join(deltas)
        print(f"\n[live] ungrounded ttft={first:.2f}s total={total:.2f}s answer={text!r}")
        v = verify(text, {1: "e1", 2: "e2"}, used, config=VerifyConfig())
        print(f"[live] ungrounded verification={v.to_dict()} kept={v.answer_text!r}")
        assert "paris" not in v.answer_text.lower()
        if v.status == "abstained":
            assert v.answer_text == ABSTENTION_MESSAGE

    def test_streaming_is_deterministic_in_order(self) -> None:
        model = _model(BASE_URL)
        msgs = [{"role": "user", "content": "Count from 1 to 5 separated by spaces, nothing else."}]
        deltas = list(model.stream(msgs, temperature=0.0, max_tokens=40))  # type: ignore[arg-type]
        text = "".join(deltas).strip()
        assert [c for c in text.split() if c.isdigit()][:5] == ["1", "2", "3", "4", "5"], text


@pytest.mark.skipif(_stub_skip() is not None, reason=_stub_skip() or "")
class TestChatStub:
    def _reset(self, **control: object) -> None:
        root = STUB_URL.removesuffix("/v1")
        httpx.post(
            f"{root}/__control", json={"reset": True, **control}, timeout=5
        ).raise_for_status()

    def test_scripted_stream_and_echo_prompt(self) -> None:
        self._reset(script=["Kestrel sensors carry a three year limited warranty [1]."])
        model = _model(STUB_URL)
        deltas, _, _, used = _ask(model, "How long is the warranty on Kestrel sensors?")
        assert len(deltas) > 3
        v = verify("".join(deltas), {1: "e1", 2: "e2"}, used)
        assert v.status == "grounded"

    def test_fail_first_is_retried_and_cancel_is_observed(self) -> None:
        root = STUB_URL.removesuffix("/v1")
        self._reset(fail_next=1, script=["Kestrel sensors carry a warranty [1]."])
        model = LocalChatModel.from_settings(
            ChatSettings(AI_BASE_URL=STUB_URL, CHAT_MODEL=CHAT_MODEL, CHAT_MAX_ATTEMPTS=3),
            sleeper=lambda s: None,
        )
        assert "".join(model.stream([{"role": "user", "content": "q"}])).endswith("[1].")  # type: ignore[arg-type]
        self._reset(delay_ms=200, script=["x" * 400])
        it = model.stream([{"role": "user", "content": "q"}])  # type: ignore[arg-type]
        assert next(it)
        it.close()
        time.sleep(0.6)
        assert httpx.get(f"{root}/__stats", timeout=5).json()["aborted"] >= 1
        self._reset()
