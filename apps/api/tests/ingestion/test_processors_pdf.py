"""PDF processor: page locators, limits and hostile files."""

from __future__ import annotations

import io
import time
import zlib

import pytest
from pypdf import PdfWriter

from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError
from app.modules.ingestion.processors import Limits
from tests.ingestion.test_processors_support import INPUTS, PDF
from tests.ingestion.test_processors_support import run as run_blocked


def pdf(data: bytes, limits: Limits | None = None) -> ExtractedDocument:
    return run_blocked(data, PDF, limits)


def blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def raw_pdf(objects: dict[int, bytes], root: int = 1) -> bytes:
    """A minimal hand-assembled PDF (no xref table: pypdf rebuilds it, like for broken files)."""
    body = b"%PDF-1.4\n"
    for number, content in objects.items():
        body += b"%d 0 obj\n" % number + content + b"\nendobj\n"
    return body + b"trailer\n<< /Root %d 0 R /Size %d >>\n%%%%EOF\n" % (root, max(objects) + 1)


def expect(data: bytes, code: str, limits: Limits | None = None) -> ProcessingError:
    with pytest.raises(ProcessingError) as info:
        pdf(data, limits)
    assert info.value.code == code, info.value
    assert info.value.permanent is True
    return info.value


def test_report_pages_and_locators() -> None:
    doc = pdf((INPUTS / "report.pdf").read_bytes())
    assert doc.modality == "pdf"
    assert doc.stats.pages == 3
    assert [b.locator.page for b in doc.blocks] == [1, 2, 3]
    assert all(b.locator.kind == "page" for b in doc.blocks)
    assert doc.blocks[0].text.startswith("Annual Orchard Report")
    stream = doc.stream()
    for block in doc.blocks:
        assert stream[block.locator.char_start : block.locator.char_end] == block.text


def test_page_break_in_sentence_keeps_both_halves_on_their_own_pages() -> None:
    doc = pdf((INPUTS / "sentence_break.pdf").read_bytes())
    assert [b.locator.page for b in doc.blocks] == [1, 2]
    assert doc.blocks[0].text.endswith("the committee decided to")
    assert doc.blocks[1].text.startswith("postpone the vote")


def test_unicode_text_is_extracted() -> None:
    text = pdf((INPUTS / "unicode.pdf").read_bytes()).stream()
    for expected in ("Γεια", "Привет", "café", "Łódź"):
        assert expected in text


def test_too_many_pages_5000_blank_pages() -> None:
    started = time.perf_counter()
    expect(blank_pdf(5000), "too_many_pages")
    assert time.perf_counter() - started < 30


def test_page_limit_is_configurable_and_exact() -> None:
    expect(blank_pdf(3), "too_many_pages", Limits(max_pages=2))
    # exactly at the limit is accepted by the page check (then it has no text)
    expect(blank_pdf(2), "no_text_extracted", Limits(max_pages=2))


def test_pdf_with_no_text_is_no_text_extracted() -> None:
    expect(blank_pdf(3), "no_text_extracted")


def test_encrypted_pdf_is_unreadable() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("user-password", "owner-password")
    out = io.BytesIO()
    writer.write(out)
    expect(out.getvalue(), "pdf_unreadable")


def test_encrypted_with_empty_user_password_is_still_refused() -> None:
    writer = PdfWriter(clone_from=io.BytesIO((INPUTS / "report.pdf").read_bytes()))
    writer.encrypt("", "owner-password")
    out = io.BytesIO()
    writer.write(out)
    expect(out.getvalue(), "pdf_unreadable")


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-1.7\n" + bytes(range(256)) * 40,
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n",
        b"not a pdf at all, just words",
        b"\x00\x01\x02" * 200,
    ],
)
def test_garbled_files_are_unreadable(data: bytes) -> None:
    expect(data, "pdf_unreadable")


def test_truncated_pdf_does_not_crash() -> None:
    good = (INPUTS / "report.pdf").read_bytes()
    for cut in (len(good) // 2, len(good) - 40, 60):
        with pytest.raises(ProcessingError) as info:
            pdf(good[:cut])
        assert info.value.code in {"pdf_unreadable", "no_text_extracted"}


def test_header_must_be_near_the_start() -> None:
    expect(b"x" * 5000 + (INPUTS / "report.pdf").read_bytes(), "pdf_unreadable")


def test_empty_input_is_no_text() -> None:
    expect(b"", "no_text_extracted")
    expect(b"  \n", "no_text_extracted")


def test_input_size_limit() -> None:
    data = (INPUTS / "report.pdf").read_bytes()
    expect(data, "too_large", Limits(max_bytes=len(data) - 1))


def test_cyclic_page_tree_terminates_with_a_stable_error() -> None:
    data = raw_pdf(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>",
        }
    )
    started = time.perf_counter()
    with pytest.raises(ProcessingError) as info:
        pdf(data)
    assert info.value.code in {"pdf_unreadable", "no_text_extracted", "too_many_pages"}
    assert time.perf_counter() - started < 10


def test_lying_page_count_is_rejected_by_the_limit() -> None:
    data = raw_pdf(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 999999999 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        }
    )
    with pytest.raises(ProcessingError) as info:
        pdf(data)
    assert info.value.code in {"too_many_pages", "pdf_unreadable"}


def test_decompression_bomb_ends_in_a_controlled_error() -> None:
    bomb = zlib.compress(b"0" * (120 * 1024 * 1024), 9)
    assert len(bomb) < 1_000_000
    data = raw_pdf(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>",
            4: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(bomb)
            + bomb
            + b"\nendstream",
        }
    )
    started = time.perf_counter()
    with pytest.raises(ProcessingError) as info:
        pdf(data)
    assert info.value.code in {"pdf_unreadable", "no_text_extracted", "too_large"}
    assert time.perf_counter() - started < 60


def test_extracted_text_limit_is_enforced_for_small_highly_compressible_input() -> None:
    from fpdf import FPDF

    doc = FPDF()
    doc.set_auto_page_break(auto=False)
    doc.add_page()
    doc.set_font("Helvetica", "", 6)
    doc.multi_cell(0, 3, "lorem ipsum " * 3000)
    data = bytes(doc.output())
    text_chars = len(pdf(data).stream())
    assert text_chars > 20_000
    assert len(data) < 20_000  # the file is under the limit, its text is not
    expect(data, "too_large", Limits(max_bytes=20_000))


def test_block_limit_applies_to_pdf_paragraphs() -> None:
    expect((INPUTS / "report.pdf").read_bytes(), "too_large", Limits(max_blocks=1))
