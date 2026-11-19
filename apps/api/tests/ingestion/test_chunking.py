"""Normalization and chunking (M03.02.03): stable chunks and exact source offsets."""

from __future__ import annotations

import hashlib
import math
import uuid
from itertools import pairwise

import pytest

from app.modules.ingestion.chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_CHARS,
    DEFAULT_TARGET_CHARS,
    chunk_document,
    normalize_block_text,
    normalize_text,
)
from app.modules.ingestion.contracts import (
    Block,
    BlockKind,
    Chunk,
    ExtractedDocument,
    Locator,
    ProcessingError,
    ProcessorInfo,
    SourceInfo,
    Stats,
)
from app.modules.ingestion.ids import PIPELINE_VERSION, chunk_id_for, revision_id_for

REVISION = revision_id_for(uuid.UUID(int=7), PIPELINE_VERSION, "etest")


def make_doc(
    blocks: list[tuple[str, str]],
    *,
    modality: str = "markdown",
    pages: list[int] | None = None,
    paths: list[list[str]] | None = None,
    normalize: bool = True,
) -> ExtractedDocument:
    """Build a valid document from ``(kind, text)`` pairs (normalized unless told otherwise)."""
    built: list[Block] = []
    pos = 0
    for i, (kind, raw_text) in enumerate(blocks):
        text = normalize_block_text(raw_text, kind) if normalize else raw_text
        loc_kind = {"pdf": "page", "html": "offset"}.get(modality, "line")
        built.append(
            Block(
                block_id=f"b{i}",
                kind=kind,  # type: ignore[arg-type]
                text=text,
                level=1 if kind == "heading" else None,
                locator=Locator(
                    kind=loc_kind,  # type: ignore[arg-type]
                    page=pages[i] if pages else None,
                    char_start=pos,
                    char_end=pos + len(text),
                    line_start=i + 1 if loc_kind == "line" else None,
                    line_end=i + 1 if loc_kind == "line" else None,
                    section_path=paths[i] if paths else None,
                ),
            )
        )
        pos += len(text) + 2
    return ExtractedDocument(
        modality=modality,  # type: ignore[arg-type]
        source=SourceInfo(
            version_id=uuid.UUID(int=7), sha256="b" * 64, content_type="text/plain", byte_size=1
        ),
        processor=ProcessorInfo(name="t", version="1"),
        blocks=built,
        stats=Stats(pages=max(pages) if pages else None, chars=max(pos - 2, 0)),
    )


def para(*texts: str) -> ExtractedDocument:
    return make_doc([("paragraph", t) for t in texts])


def check_invariants(
    doc: ExtractedDocument,
    chunks: list[Chunk],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    revision: uuid.UUID = REVISION,
) -> None:
    stream = doc.stream()
    assert chunks
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    for chunk in chunks:
        assert chunk.chunk_id == chunk_id_for(revision, chunk.chunk_index)
        assert chunk.text.strip()
        assert len(chunk.text) <= max_chars
        assert chunk.token_estimate == math.ceil(len(chunk.text) / 4)
        assert chunk.content_hash == hashlib.sha256(chunk.text.encode()).hexdigest()
        assert chunk.text == "\n\n".join(
            stream[ln.char_start : ln.char_end] for ln in chunk.locators
        )
        assert chunk.char_start == chunk.locators[0].char_start
        assert chunk.char_end == chunk.locators[-1].char_end


# --- normalization --------------------------------------------------------------------------------


def test_normalize_collapses_whitespace_keeps_paragraph_breaks() -> None:
    assert normalize_text("  a \t b  \r\n  c  \n\n\n\n d \x00\x07 e  f  ") == "a b\nc\n\nd e f"


def test_normalize_is_nfc_and_strips_controls() -> None:
    assert normalize_text("é\x00​") == "é​"  # zero-width space is kept
    assert normalize_text("﻿bom￾") == "bom"
    assert normalize_text("a\ud800b") == "ab"  # lone surrogate removed


def test_normalize_layout_mode_keeps_indentation() -> None:
    src = "\n\n  def f():\n\treturn 1   \n\n    pass\n\n"
    assert normalize_text(src, preserve_layout=True) == "  def f():\n    return 1\n\n    pass"


@pytest.mark.parametrize("layout", [False, True])
def test_normalize_is_idempotent_on_awkward_input(layout: bool) -> None:
    for src in ("e\x00́ x", " \n \n a", "  x\x0by", "a\t\tb\r\rc", "﻿　z"):
        once = normalize_text(src, preserve_layout=layout)
        assert normalize_text(once, preserve_layout=layout) == once


# --- chunking basics ------------------------------------------------------------------------------


def test_defaults_match_the_contract() -> None:
    assert (DEFAULT_TARGET_CHARS, DEFAULT_OVERLAP_CHARS, DEFAULT_MAX_CHARS) == (1800, 200, 2400)


def test_small_document_is_one_chunk_with_all_block_locators() -> None:
    doc = para("alpha", "beta", "gamma")
    (chunk,) = chunk_document(doc, REVISION)
    assert chunk.text == "alpha\n\nbeta\n\ngamma"
    assert [(ln.char_start, ln.char_end) for ln in chunk.locators] == [(0, 5), (7, 11), (13, 18)]
    assert chunk.chunk_id == chunk_id_for(REVISION, 0)
    assert chunk.token_estimate == math.ceil(len(chunk.text) / 4)
    assert chunk.content_hash == hashlib.sha256(chunk.text.encode()).hexdigest()
    check_invariants(doc, [chunk])


def test_blocks_are_packed_up_to_the_target_and_never_split_when_small() -> None:
    doc = para(*[f"paragraph {i} " + "x" * 80 for i in range(20)])
    chunks = chunk_document(doc, REVISION, target_chars=300, overlap_chars=50, max_chars=500)
    check_invariants(doc, chunks, max_chars=500)
    assert len(chunks) > 3
    all_block_texts = {b.text for b in doc.blocks}
    for chunk in chunks:  # every chunk is a whole number of blocks
        assert all(part in all_block_texts for part in chunk.text.split("\n\n"))
        assert len(chunk.text) <= 300


def test_long_block_is_cut_at_sentence_boundaries_with_overlap() -> None:
    sentences = [f"Sentence number {i} is here." for i in range(60)]
    doc = para(" ".join(sentences))
    chunks = chunk_document(doc, REVISION, target_chars=300, overlap_chars=60, max_chars=400)
    check_invariants(doc, chunks, max_chars=400)
    assert len(chunks) > 4
    for chunk in chunks:
        assert chunk.text.startswith("Sentence")
        assert chunk.text.endswith("here.")  # cut on a sentence boundary
    # consecutive chunks overlap and together cover the whole block
    for a, b in pairwise(chunks):
        assert b.char_start < a.char_end
        assert b.char_start > a.char_start
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(doc.blocks[0].text)


def test_overlap_only_exists_inside_one_long_block() -> None:
    doc = para("short one", "short two " * 5, "x " * 600, "short three")
    chunks = chunk_document(doc, REVISION, target_chars=200, overlap_chars=40, max_chars=300)
    check_invariants(doc, chunks, max_chars=300)
    long_block = doc.blocks[2].locator
    for a, b in pairwise(chunks):
        if b.char_start < a.char_end:  # overlapping ranges can only be inside the long block
            assert long_block.char_start <= b.char_start < a.char_end <= long_block.char_end


def test_block_between_target_and_max_stays_whole() -> None:
    doc = para("y" * 500, "z" * 100)
    chunks = chunk_document(doc, REVISION, target_chars=300, overlap_chars=0, max_chars=600)
    assert [len(c.text) for c in chunks] == [500, 100]


def test_hard_cut_when_no_whitespace_exists() -> None:
    doc = para("a" * 5000)
    chunks = chunk_document(doc, REVISION, target_chars=1000, overlap_chars=100, max_chars=1200)
    check_invariants(doc, chunks, max_chars=1200)
    assert all(len(c.text) <= 1000 for c in chunks)
    assert chunks[-1].char_end == 5000


def test_cuts_prefer_sentence_then_whitespace() -> None:
    doc = para("word " * 100 + "End of sentence. " + "more " * 100)
    chunks = chunk_document(doc, REVISION, target_chars=520, overlap_chars=0, max_chars=700)
    assert chunks[0].text.endswith("End of sentence.")
    check_invariants(doc, chunks, max_chars=700)


def test_heading_is_not_stranded_at_the_end_of_a_chunk() -> None:
    blocks = [
        ("paragraph", "p" * 250),
        ("heading", "Next section"),
        ("paragraph", "q" * 100),
    ]
    doc = make_doc(blocks)
    chunks = chunk_document(doc, REVISION, target_chars=300, overlap_chars=0, max_chars=500)
    assert [c.text.startswith("Next section") for c in chunks] == [False, True]
    check_invariants(doc, chunks, max_chars=500)


def test_heading_path_and_pages_come_from_the_blocks() -> None:
    doc = make_doc(
        [("heading", "Intro"), ("paragraph", "a" * 250), ("paragraph", "b" * 250)],
        modality="pdf",
        pages=[1, 1, 2],
        paths=[["Intro"], ["Intro"], ["Intro", "Deep"]],
    )
    chunks = chunk_document(doc, REVISION, target_chars=300, overlap_chars=0, max_chars=500)
    assert len(chunks) == 2
    assert chunks[0].heading_path == ["Intro"]
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)
    assert chunks[1].heading_path == ["Intro", "Deep"]
    assert (chunks[1].page_start, chunks[1].page_end) == (2, 2)
    spanning = chunk_document(doc, REVISION)[0]
    assert (spanning.page_start, spanning.page_end) == (1, 2)
    assert [ln.page for ln in spanning.locators] == [1, 1, 2]


def test_chunks_without_pages_have_no_page_range() -> None:
    (chunk,) = chunk_document(para("text"), REVISION)
    assert chunk.page_start is None
    assert chunk.page_end is None


def test_ids_derive_from_revision_and_index_only() -> None:
    doc = para(*[f"block {i} " + "w " * 150 for i in range(10)])
    one = chunk_document(doc, REVISION, target_chars=400, overlap_chars=50, max_chars=600)
    other = chunk_document(
        doc, revision_id_for(uuid.UUID(int=8), PIPELINE_VERSION, "etest"),
        target_chars=400, overlap_chars=50, max_chars=600,
    )  # fmt: skip
    assert [c.text for c in one] == [c.text for c in other]
    assert {c.chunk_id for c in one}.isdisjoint({c.chunk_id for c in other})


def test_output_is_byte_identical_across_runs() -> None:
    doc = para(*["Sentence one. Sentence two! Sentence three? " * 30 for _ in range(8)])
    a = [
        c.to_canonical_json()
        for c in chunk_document(doc, REVISION, target_chars=500, overlap_chars=80, max_chars=700)
    ]
    b = [
        c.to_canonical_json()
        for c in chunk_document(doc, REVISION, target_chars=500, overlap_chars=80, max_chars=700)
    ]
    assert a == b


# --- failures ----------------------------------------------------------------------------


def test_too_many_chunks_is_too_large() -> None:
    doc = para(*[f"paragraph {i} " + "x" * 200 for i in range(50)])
    with pytest.raises(ProcessingError) as info:
        chunk_document(
            doc, REVISION, target_chars=250, overlap_chars=0, max_chars=300, max_chunks=10
        )
    assert info.value.code == "too_large"
    assert (
        len(
            chunk_document(
                doc, REVISION, target_chars=250, overlap_chars=0, max_chars=300, max_chunks=50
            )
        )
        == 50
    )


def test_empty_document_is_no_text_extracted() -> None:
    empty = ExtractedDocument(
        modality="text",
        source=SourceInfo(
            version_id=uuid.UUID(int=1), sha256="c" * 64, content_type="text/plain", byte_size=0
        ),
        processor=ProcessorInfo(name="t", version="1"),
        blocks=[],
        stats=Stats(chars=0),
    )
    with pytest.raises(ProcessingError) as info:
        chunk_document(empty, REVISION)
    assert info.value.code == "no_text_extracted"


def test_unnormalized_block_text_is_rejected() -> None:
    doc = make_doc([("paragraph", "two  spaces")], normalize=False)
    with pytest.raises(ProcessingError) as info:
        chunk_document(doc, REVISION)
    assert info.value.code == "malformed_input"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_chars": 0},
        {"target_chars": 500, "max_chars": 400},
        {"overlap_chars": -1},
        {"target_chars": 100, "overlap_chars": 51},
        {"max_chunks": 0},
    ],
)
def test_bad_parameters_are_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match=r"require|max_chunks"):
        chunk_document(para("text"), REVISION, **kwargs)


def test_code_blocks_keep_their_layout_through_chunking() -> None:
    code: BlockKind = "code"
    doc = make_doc([("paragraph", "intro"), (code, "def f():\n    return 1\n\n    pass")])
    (chunk,) = chunk_document(doc, REVISION)
    assert "    return 1\n\n    pass" in chunk.text
    check_invariants(doc, [chunk])
