"""Plain-text processor, encoding handling and the processor registry."""

from __future__ import annotations

import codecs
import os

import pytest

from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError, SourceInfo
from app.modules.ingestion.processors import (
    SUPPORTED_CONTENT_TYPES,
    Limits,
    get_processor,
    normalize_content_type,
)
from tests.ingestion.test_processors_support import TEXT
from tests.ingestion.test_processors_support import run as run_blocked


def run(data: bytes, content_type: str = TEXT, limits: Limits | None = None) -> ExtractedDocument:
    """Extract with sockets disabled (and any attempt asserted absent)."""
    return run_blocked(data, content_type, limits)


def texts(data: bytes, content_type: str = TEXT) -> list[str]:
    return [b.text for b in run(data, content_type).blocks]


# --- registry -----------------------------------------------------------------------------------


def test_registry_covers_the_supported_types_case_insensitively() -> None:
    assert {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/html",
    } <= SUPPORTED_CONTENT_TYPES
    assert get_processor("TEXT/Plain; charset=UTF-8").name == "text"
    assert get_processor(" application/PDF ").name == "pdf"
    assert get_processor("text/markdown").modality == "markdown"
    assert get_processor("text/html;charset=latin-1").modality == "html"
    assert normalize_content_type("Text/HTML ; Charset=x") == "text/html"


@pytest.mark.parametrize(
    "content_type",
    ["image/gif", "application/zip", "", "text", "application/octet-stream", "text/rtf"],
)
def test_unknown_types_are_a_permanent_unsupported_type(content_type: str) -> None:
    with pytest.raises(ProcessingError) as info:
        get_processor(content_type)
    assert info.value.code == "unsupported_type"
    assert info.value.permanent is True
    with pytest.raises(ProcessingError) as info2:
        run(b"data", content_type)
    assert info2.value.code == "unsupported_type"


def test_extract_maps_unexpected_exceptions_to_malformed_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.ingestion.processors.registry as registry

    def boom(
        data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
    ) -> ExtractedDocument:
        raise RuntimeError("secret document text must not leak")

    monkeypatch.setattr(
        registry, "_REGISTRY", {"text/plain": registry.Processor("t", "1", "text", boom)}
    )
    with pytest.raises(ProcessingError) as info:
        run(b"hello")
    assert info.value.code == "malformed_input"
    assert "secret" not in str(info.value)


# --- basic extraction ---------------------------------------------------------------------------


def test_paragraphs_and_line_locators() -> None:
    doc = run(b"first line\nsecond line\n\n\n\nthird paragraph\n")
    assert doc.modality == "text"
    assert [b.text for b in doc.blocks] == ["first line\nsecond line", "third paragraph"]
    assert [(b.locator.line_start, b.locator.line_end) for b in doc.blocks] == [(1, 2), (6, 6)]
    assert doc.processor.name == "text"
    assert doc.stats.chars == len(doc.stream())


def test_whitespace_is_normalized() -> None:
    assert texts(b"a   b\t\tc\n  indented   line  \n") == ["a b c\nindented line"]


def test_mixed_newlines_are_unified_and_counted_once() -> None:
    doc = run(b"one\r\ntwo\rthree\n\r\nfour\x0cfive\n")
    assert [b.text for b in doc.blocks] == ["one\ntwo\nthree", "four\nfive"]
    assert doc.blocks[0].locator.line_end == 3
    assert doc.blocks[1].locator.line_start == 5


def test_nul_and_control_characters_are_stripped() -> None:
    assert texts(b"he\x00llo\x01 wor\x1bld\x7f!") == ["hello world!"]


def test_utf8_bom_is_removed() -> None:
    assert texts(codecs.BOM_UTF8 + "café".encode()) == ["café"]


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("utf-32-le", codecs.BOM_UTF32_LE),
        ("utf-32-be", codecs.BOM_UTF32_BE),
    ],
)
def test_bom_variants(encoding: str, bom: bytes) -> None:
    assert texts(bom + "naïve € text\n\nsecond".encode(encoding)) == [
        "naïve € text",
        "second",
    ]


def test_declared_charset_is_used_for_legacy_bytes() -> None:
    data = "grüße aus München".encode("cp1252")
    assert texts(data, "text/plain; charset=windows-1252") == ["grüße aus München"]


def test_unknown_declared_charset_falls_back_to_detection() -> None:
    data = "Le café est prêt, à bientôt mon ami et à demain".encode("latin-1")
    assert "café" in texts(data, "text/plain; charset=made-up-charset")[0]


def test_invalid_utf8_without_a_usable_encoding_is_encoding_unreadable() -> None:
    data = os.urandom(0) + bytes([0x80, 0x81, 0xFE, 0xFF] * 200)
    with pytest.raises(ProcessingError) as info:
        run(data)
    assert info.value.code in {"encoding_unreadable"}


def test_binary_garbage_is_not_text() -> None:
    data = b"\x00\x01\x02\x03\x04\x05\x06\x07" * 500
    with pytest.raises(ProcessingError) as info:
        run(data)
    assert info.value.code == "encoding_unreadable"


def test_truncated_utf16_is_encoding_unreadable() -> None:
    with pytest.raises(ProcessingError) as info:
        run(codecs.BOM_UTF16_LE + "abc".encode("utf-16-le") + b"\x61")
    assert info.value.code == "encoding_unreadable"


def test_rtl_and_emoji_text_round_trips() -> None:
    text = "שלום עולם مرحبا \U0001f600 \U0001f468‍\U0001f469‍\U0001f467"
    assert texts(text.encode()) == [text]


def test_combining_characters_are_composed_to_nfc() -> None:
    assert texts("café".encode()) == ["café"]


@pytest.mark.parametrize("data", [b"", b"   ", b"\n\n\t \r\n", b"\xc2\xa0 \xc2\xa0"])
def test_empty_or_whitespace_only_is_no_text_extracted(data: bytes) -> None:
    with pytest.raises(ProcessingError) as info:
        run(data)
    assert info.value.code == "no_text_extracted"
    assert info.value.permanent is True


def test_whitespace_only_utf16_is_no_text_extracted() -> None:
    with pytest.raises(ProcessingError) as info:
        run(codecs.BOM_UTF16_LE + "  \n ".encode("utf-16-le"))
    assert info.value.code == "no_text_extracted"


def test_huge_single_line_is_one_block() -> None:
    line = ("word " * 400_000).encode()  # 2 MB, one line
    doc = run(line)
    assert len(doc.blocks) == 1
    assert doc.blocks[0].locator.line_start == doc.blocks[0].locator.line_end == 1


def test_input_over_the_limit_is_too_large_before_decoding() -> None:
    with pytest.raises(ProcessingError) as info:
        run(b"a" * 101, limits=Limits(max_bytes=100))
    assert info.value.code == "too_large"
    assert run(b"a" * 100, limits=Limits(max_bytes=100)).stats.chars == 100


def test_too_many_blocks_is_too_large() -> None:
    data = b"\n\n".join(b"p" for _ in range(11))
    with pytest.raises(ProcessingError) as info:
        run(data, limits=Limits(max_blocks=10))
    assert info.value.code == "too_large"


def test_limits_validate() -> None:
    with pytest.raises(ValueError, match="positive"):
        Limits(max_pages=0)
    with pytest.raises(ValueError, match="ceiling"):
        Limits(max_blocks=10**9)


def test_only_nul_bytes_is_binary_not_text() -> None:
    with pytest.raises(ProcessingError) as info:
        run(b"\x00\x00\x00\x00")
    assert info.value.code == "encoding_unreadable"
