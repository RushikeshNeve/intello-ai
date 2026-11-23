"""M13.02 built-in redactor and hard length limits (pure functions)."""

from __future__ import annotations

import pytest

from app.modules.observability import capture


def test_builtin_redactor_masks_emails_numbers_and_keys() -> None:
    out = capture.builtin_redact(
        "jane@example.com 4111 1111 1111 1111 sk-abcdefghijklmnop1234 Bearer abcdefghijklmnopqrst"
    )
    assert "jane@" not in out
    assert "4111" not in out
    assert "sk-abc" not in out
    assert "abcdefghijklmnopqrst" not in out
    assert capture.builtin_redact("order 12 of 34, call us") == "order 12 of 34, call us"


def test_redact_uses_firewall_pii_when_present() -> None:
    out = capture.redact("mail a@b.io")  # M14.04 module present: its redacted text, not a repr
    assert out == "mail [EMAIL_1]"
    assert "0123456789012" not in capture.redact("account 0123456789012")  # regex still on top


def test_redact_falls_back_without_firewall_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "_firewall_redactor", lambda: None)
    assert capture.redact("mail a@b.io") == "mail [EMAIL]"


def test_redact_falls_back_when_firewall_pii_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_value: str) -> str:
        raise RuntimeError

    monkeypatch.setattr(capture, "_firewall_redactor", lambda: boom)
    assert capture.redact("mail a@b.io") == "mail [EMAIL]"


def test_limit_is_hard() -> None:
    assert len(capture._limit("x" * 50_000, capture.MAX_ANSWER_CHARS)) == capture.MAX_ANSWER_CHARS
