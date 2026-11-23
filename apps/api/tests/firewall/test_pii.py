"""M14.04: detectors (positive + false-positive guards), redaction, outbound guard, ingestion."""

from __future__ import annotations

import json
from typing import Literal
from collections.abc import Iterator, Sequence
from uuid import uuid4

import httpx
import pytest

from app.modules.ai.gateway import InMemoryPolicySource, ModelPolicy, get_chat_model
from app.modules.ai.models.pii_guard import PiiBlocked, PiiGuardedChatModel
from app.modules.ai.models.types import ChatMessage, ModelSpec
from app.modules.firewall.pii import PiiPolicy, Redactor, count_by_type, detect, redact
from app.modules.firewall.pii.detectors import SECRET_TYPES, verhoeff_ok
from app.modules.firewall.pii.ingest import IngestPiiSettings, scan_text
from tests.gateway.support import settings


def types_of(text: str) -> list[str]:
    return [f.type for f in detect(text)]


def _aadhaar() -> str:
    body = "23456789012"
    check = next(d for d in "0123456789" if verhoeff_ok(body + d))
    return body + check


AADHAAR = _aadhaar()
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34Gk\n-----END RSA PRIVATE KEY-----"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1rwW1gFWFOEjXk"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail jane.doe+x@example.co.uk now", "EMAIL"),
        ("call +91 98765 43210 today", "PHONE"),
        ("call +1 (415) 555-0132", "PHONE"),
        ("host 192.168.10.25 up", "IPV4"),
        ("addr 2001:db8::8a2e:370:7334", "IPV6"),
        ("card 4111 1111 1111 1111", "CREDIT_CARD"),
        ("iban GB82 WEST 1234 5698 7654 32 is mine", "IBAN"),
        ("ssn 123-45-6789", "US_SSN"),
        (f"aadhaar {AADHAAR}", "AADHAAR"),
        ("pan ABCPE1234F", "PAN"),
        ("Passport No: K1234567", "PASSPORT"),
        (f"token {JWT}", "JWT"),
        ("k AKIAIOSFODNN7EXAMPLE", "AWS_KEY"),
        ("k AIza" + "a1B2c3D4e5" * 3 + "a1B2c", "GCP_KEY"),
        ("t ghp_" + "a1B2" * 9, "GITHUB_TOKEN"),
        ("t xoxb-123456789012-abcdefghijkl", "SLACK_TOKEN"),
        ("k sk-" + "A1b2C3d4" * 4, "OPENAI_KEY"),
        ("k sk-ant-" + "A1b2C3d4" * 4, "ANTHROPIC_KEY"),
        ("k sk_live_" + "a1B2c3D4" * 2, "STRIPE_KEY"),
        (PEM, "PRIVATE_KEY"),
        ('api_key = "k3J9xQ2mVb7LpZ4wRt8N"', "SECRET"),
    ],
)
def test_detector_positive(text: str, expected: str) -> None:
    assert expected in types_of(text)


@pytest.mark.parametrize(
    "text",
    [
        "order 4111 1111 1111 1112 shipped",  # fails Luhn
        "id 1234567890123456",  # random 16 digits, fails Luhn
        "iban GB82 WEST 1234 5698 7654 33",  # bad mod-97
        "number 234567890123 here",  # bare 12 digits, no context
        f"aadhaar 2345 6789 0123 ({AADHAAR[:-1]}x)",  # bad Verhoeff group
        "time 12:30:45 and ratio 3:2",  # not IPv6
        "version 1.2.3 and 10.0.0.256",  # not IPv4
        "date 2024-01-15 and 000-12-3456 and 666-12-3456",  # not SSN
        "ABCDE1234 and ABCXE1234F",  # not PAN shapes
        "passport renewal takes weeks",  # context word without an id
        "password: hunter",  # short, low entropy
        "token = aaaaaaaaaaaaaaaaaaaa",  # low entropy
        "see the sk-learn docs and mail us at team@",  # prefixes without keys
        "call 12345",  # short digits
    ],
)
def test_detector_negative(text: str) -> None:
    assert detect(text) == []


def test_overlap_prefers_secret_over_shapes() -> None:
    found = detect("k sk_live_" + "a1B2c3D4" * 2 + " mail a@b.io")
    assert [f.type for f in found] == ["STRIPE_KEY", "EMAIL"]


def test_redaction_stable_placeholders_and_counts() -> None:
    r = Redactor("placeholder")
    a = r.redact("a@x.io then b@x.io then a@x.io, ip 10.1.2.3")
    assert a.text == "[EMAIL_1] then [EMAIL_2] then [EMAIL_1], ip [IPV4_1]"
    assert a.counts_by_type == {"EMAIL": 3, "IPV4": 1}
    assert r.redact("again b@x.io").text == "again [EMAIL_2]"  # stable across texts in a request
    assert r.restore("[EMAIL_2] ok") == "b@x.io ok"


def test_redaction_modes_and_no_value_in_repr() -> None:
    assert redact("a@x.io", "mask").text == "[REDACTED]"
    h = redact("a@x.io", "hash").text
    assert h.startswith("[EMAIL:") and "a@x.io" not in h
    res = redact("a@x.io")
    assert "a@x.io" not in repr(res) and res.counts_by_type == {"EMAIL": 1}
    only = redact("a@x.io 10.1.2.3", policy=PiiPolicy(types=frozenset({"IPV4"})))
    assert only.text == "a@x.io [IPV4_1]"


# ---------------------------------------------------------------------- outbound


class CapturingHosted:
    """A fake hosted adapter: records everything it is sent."""

    def __init__(self, reply: str = "ok") -> None:
        self.spec = ModelSpec("openai", "fake-hosted", 8000, 500)
        self.sent: list[list[ChatMessage]] = []
        self.reply = reply

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        self.sent.append([dict(m) for m in messages])  # type: ignore[misc]
        for i in range(0, len(self.reply), 3):  # tiny deltas split placeholders
            yield self.reply[i : i + 3]

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self.stream(messages))


CANARY_EMAIL = "canary.person@example.org"
CANARY_CARD = "4111 1111 1111 1111"
PROMPT: list[ChatMessage] = [
    {"role": "system", "content": "Answer from evidence."},
    {"role": "user", "content": f"Mail {CANARY_EMAIL} card {CANARY_CARD}: who is it?"},
]


def test_outbound_redact_sends_no_raw_pii_and_restores_only_when_asked() -> None:
    hosted = CapturingHosted("Contact [EMAIL_1] about [CREDIT_CARD_1].")
    out = "".join(PiiGuardedChatModel(hosted, mode="redact").stream(PROMPT))
    blob = json.dumps(hosted.sent)
    assert CANARY_EMAIL not in blob and "4111" not in blob and "[EMAIL_1]" in blob
    assert out == "Contact [EMAIL_1] about [CREDIT_CARD_1]."  # restore defaults to false
    hosted2 = CapturingHosted("Contact [EMAIL_1] about [CREDIT_CARD_1].")
    guarded = PiiGuardedChatModel(hosted2, mode="redact", restore=True)
    assert guarded.complete(PROMPT) == f"Contact {CANARY_EMAIL} about {CANARY_CARD}."
    assert CANARY_EMAIL not in json.dumps(hosted2.sent)  # the mapping never goes out


def test_outbound_secret_blocks_by_default_and_block_mode_blocks_pii() -> None:
    hosted = CapturingHosted()
    secret_prompt: list[ChatMessage] = [{"role": "user", "content": "key sk-" + "A1b2C3d4" * 4}]
    with pytest.raises(PiiBlocked) as exc:
        PiiGuardedChatModel(hosted, mode="redact").complete(secret_prompt)
    assert exc.value.code == "pii_blocked" and "OPENAI_KEY" in exc.value.counts_by_type
    assert "A1b2C3d4" not in str(exc.value) and hosted.sent == []
    with pytest.raises(PiiBlocked):
        PiiGuardedChatModel(hosted, mode="block").complete(PROMPT)
    assert hosted.sent == []
    ok = PiiGuardedChatModel(hosted, mode="redact", secrets="redact")
    ok.complete(secret_prompt)
    assert "sk-" not in json.dumps(hosted.sent)


def test_gateway_wraps_only_hosted_and_only_when_enabled() -> None:
    org = uuid4()
    s = settings(AI_EXTERNAL_PROVIDERS_ENABLED=True, OPENAI_API_KEY="sk-test-x" * 3)
    seen: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.content)
        body = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
        return httpx.Response(200, content=body + b"data: [DONE]\n\n")

    def make(policy: ModelPolicy, provider: Literal["local", "openai"]) -> object:
        src = InMemoryPolicySource({org: policy})
        return get_chat_model(
            src,
            org,
            s,
            provider=provider,
            transport=httpx.MockTransport(handler),
        )

    both = frozenset({"local", "openai"})
    assert not isinstance(make(ModelPolicy(True, both), "openai"), PiiGuardedChatModel)  # off
    guarded = make(ModelPolicy(True, both, pii_outbound="redact"), "openai")
    assert isinstance(guarded, PiiGuardedChatModel)
    assert guarded.complete(PROMPT) == "hi"
    assert CANARY_EMAIL.encode() not in seen[0] and b"[EMAIL_1]" in seen[0]
    local = make(ModelPolicy(True, both, pii_outbound="block"), "local")
    assert not isinstance(local, PiiGuardedChatModel)  # local path untouched


# ---------------------------------------------------------------------- ingestion


def test_ingestion_counts_only_and_optional_masking() -> None:
    text = f"mail {CANARY_EMAIL} and {CANARY_EMAIL}, ssn 123-45-6789"
    out, counts = scan_text(text, IngestPiiSettings())
    assert out == text and counts == {}  # default off
    out, counts = scan_text(text, IngestPiiSettings(record_counts=True))
    assert out == text and counts == {"EMAIL": 2, "US_SSN": 1}
    assert CANARY_EMAIL not in json.dumps(counts)
    out, counts = scan_text(text, IngestPiiSettings(record_counts=True, mask_indexed=True))
    assert CANARY_EMAIL not in out and "123-45-6789" not in out and counts["EMAIL"] == 2
    assert count_by_type("nothing here") == {}


def test_secret_types_are_flagged() -> None:
    assert "PRIVATE_KEY" in SECRET_TYPES and "EMAIL" not in SECRET_TYPES
