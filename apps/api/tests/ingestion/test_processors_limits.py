"""Size limits and linear-time smoke tests of the processors and the chunker."""

from __future__ import annotations

import time
import uuid

import pytest

from app.modules.ingestion.chunking import chunk_document
from app.modules.ingestion.contracts import ProcessingError
from app.modules.ingestion.processors import MAX_EXTRACT_BYTES, MAX_PDF_PAGES
from tests.ingestion.test_processors_support import HTML, MARKDOWN, TEXT
from tests.ingestion.test_processors_support import run as run_blocked

MIB = 1024 * 1024


def test_default_limits_are_the_contract_values() -> None:
    assert MAX_PDF_PAGES == 2000
    assert MAX_EXTRACT_BYTES == 50 * MIB


@pytest.mark.parametrize("content_type", [TEXT, MARKDOWN, HTML])
def test_input_over_50_mib_is_too_large(content_type: str) -> None:
    started = time.perf_counter()
    with pytest.raises(ProcessingError) as info:
        run_blocked(b"a" * (50 * MIB + 1), content_type)
    assert info.value.code == "too_large"
    assert info.value.permanent is True
    assert time.perf_counter() - started < 2  # rejected before any decoding


def test_text_of_exactly_50_mib_is_accepted() -> None:
    line = ("0123456789" * 7 + "\n") * 1  # 71 bytes
    body = (line * (50 * MIB // len(line) + 1))[: 50 * MIB]
    doc = run_blocked(body.encode(), TEXT)
    assert doc.stats.chars <= 50 * MIB


def _prose(size: int) -> bytes:
    sentence = "The quick brown fox jumps over the lazy dog. "
    paragraph = (sentence * 6).strip() + "\n\n"
    return (paragraph * (size // len(paragraph) + 1))[:size].encode()


def test_5_mib_text_is_processed_in_linear_time() -> None:
    data = _prose(5 * MIB)
    started = time.perf_counter()
    doc = run_blocked(data, TEXT)
    extract_seconds = time.perf_counter() - started
    started = time.perf_counter()
    chunks = chunk_document(doc, uuid.UUID(int=1), max_chunks=100_000)
    chunk_seconds = time.perf_counter() - started
    assert extract_seconds < 5, extract_seconds
    assert chunk_seconds < 5, chunk_seconds
    assert len(chunks) > 2000
    print(
        f"\n5 MiB text: extract {extract_seconds:.2f}s ({5 / extract_seconds:.1f} MiB/s), "
        f"chunk {chunk_seconds:.2f}s ({5 / chunk_seconds:.1f} MiB/s), {len(chunks)} chunks"
    )


def test_doubling_the_input_roughly_doubles_the_time() -> None:
    small, large = _prose(MIB), _prose(4 * MIB)
    t0 = time.perf_counter()
    run_blocked(small, TEXT)
    t1 = time.perf_counter()
    run_blocked(large, TEXT)
    t2 = time.perf_counter()
    assert (t2 - t1) < 12 * max(t1 - t0, 0.05)  # 4x input: far below quadratic (16x)


def test_chunking_a_5_mib_single_line_is_linear() -> None:
    line = ("lorem ipsum dolor sit amet " * 200_000)[: 5 * MIB].encode()
    doc = run_blocked(line, TEXT)
    assert len(doc.blocks) == 1
    started = time.perf_counter()
    chunks = chunk_document(doc, uuid.UUID(int=1), max_chunks=100_000)
    assert time.perf_counter() - started < 5
    assert max(len(c.text) for c in chunks) <= 1800


def test_chunking_a_5_mib_line_without_any_whitespace_hard_cuts() -> None:
    doc = run_blocked(b"x" * (5 * MIB), TEXT)
    started = time.perf_counter()
    chunks = chunk_document(doc, uuid.UUID(int=1), max_chunks=100_000)
    assert time.perf_counter() - started < 5
    assert all(len(c.text) <= 1800 for c in chunks)
    assert len(chunks) == len({c.chunk_id for c in chunks})


def test_markdown_and_html_5_mib_inputs_finish_quickly() -> None:
    md = ("## Heading\n\n" + "A sentence of body text. " * 40 + "\n\n") * 1500
    started = time.perf_counter()
    run_blocked(md.encode(), MARKDOWN)
    assert time.perf_counter() - started < 5
    page = ("<h2>Heading</h2><p>" + "A sentence of body text. " * 40 + "</p>") * 1500
    started = time.perf_counter()
    run_blocked(page.encode(), HTML)
    assert time.perf_counter() - started < 10
