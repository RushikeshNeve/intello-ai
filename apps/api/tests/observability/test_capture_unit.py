"""M13.02 built-in redactor and hard length limits (pure functions)."""

from __future__ import annotations

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


def test_redact_falls_back_without_firewall_module() -> None:
    assert capture.redact("mail a@b.io") == "mail [EMAIL]"


def test_limit_is_hard() -> None:
    assert len(capture._limit("x" * 50_000, capture.MAX_ANSWER_CHARS)) == capture.MAX_ANSWER_CHARS
