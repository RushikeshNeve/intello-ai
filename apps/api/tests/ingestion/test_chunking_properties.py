"""Property tests (hypothesis) of normalization and chunking."""

from __future__ import annotations

import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.modules.ingestion.chunking import chunk_document, normalize_block_text, normalize_text
from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError
from app.modules.ingestion.ids import chunk_id_for
from tests.ingestion.test_chunking import check_invariants, make_doc

REVISION = uuid.UUID("44444444-4444-4444-8444-444444444444")

# Mostly ordinary prose characters, with sentence punctuation, newlines, odd whitespace, combining
# marks, RTL letters, emoji and control characters mixed in.
ALPHABET = st.one_of(
    st.sampled_from(list("abcdefghij ABC. ! ? \n\n,;:'\"()")),
    st.sampled_from(["\t", " ", "\r", "\x00", "\x0b", "́", "א", "\U0001f600", "‍"]),
    st.characters(blacklist_categories=["Cs"]),
)
KINDS = st.sampled_from(["paragraph", "heading", "list", "table", "code", "quote"])


@st.composite
def documents(draw: st.DrawFn) -> ExtractedDocument:
    blocks: list[tuple[str, str]] = []
    for _ in range(draw(st.integers(1, 12))):
        kind = draw(KINDS)
        raw = draw(st.text(ALPHABET, min_size=0, max_size=draw(st.sampled_from([20, 300, 3000]))))
        text = normalize_block_text(raw, kind)
        if text.strip():
            blocks.append((kind, text))
    if not blocks:
        blocks.append(("paragraph", "fallback text"))
    return make_doc(blocks)


@st.composite
def params(draw: st.DrawFn) -> dict[str, int]:
    target = draw(st.integers(8, 1200))
    return {
        "target_chars": target,
        "overlap_chars": draw(st.integers(0, target // 2)),
        "max_chars": draw(st.integers(target, target * 2)),
    }


SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(documents(), params())
def test_chunk_invariants_hold_for_any_document(doc: ExtractedDocument, p: dict[str, int]) -> None:
    chunks = chunk_document(doc, REVISION, max_chunks=1_000_000, **p)
    check_invariants(
        doc, chunks, max_chars=p["max_chars"], revision=REVISION
    )  # offsets, no empty, max_chars, ids
    # every part of the document text is covered by some chunk
    covered = [False] * len(doc.stream())
    for chunk in chunks:
        for loc in chunk.locators:
            for i in range(loc.char_start, loc.char_end):
                covered[i] = True
    stream = doc.stream()
    assert all(c or stream[i].isspace() for i, c in enumerate(covered))


@SETTINGS
@given(documents(), params())
def test_chunking_is_deterministic_and_ids_are_stable(
    doc: ExtractedDocument, p: dict[str, int]
) -> None:
    first = chunk_document(doc, REVISION, max_chunks=1_000_000, **p)
    second = chunk_document(doc, REVISION, max_chunks=1_000_000, **p)
    assert [c.to_canonical_json() for c in first] == [c.to_canonical_json() for c in second]
    assert [c.chunk_id for c in first] == [chunk_id_for(REVISION, i) for i in range(len(first))]
    again = ExtractedDocument.from_json(doc.to_canonical_json())  # via the JSON artifact
    third = chunk_document(again, REVISION, max_chunks=1_000_000, **p)
    assert [c.to_canonical_json() for c in third] == [c.to_canonical_json() for c in first]


@SETTINGS
@given(st.text(ALPHABET, max_size=2000), st.booleans())
def test_normalize_text_is_idempotent_and_clean(raw: str, layout: bool) -> None:
    once = normalize_text(raw, preserve_layout=layout)
    assert normalize_text(once, preserve_layout=layout) == once
    assert "\x00" not in once
    assert "\r" not in once
    assert "\n\n\n" not in once or layout
    once.encode("utf-8")  # always encodable: no lone surrogates survive
    assert once == once.strip(" \n") or layout


@SETTINGS
@given(st.text(ALPHABET, min_size=1, max_size=4000), params())
def test_a_single_long_block_never_yields_an_empty_or_oversized_chunk(
    raw: str, p: dict[str, int]
) -> None:
    text = normalize_text(raw)
    if not text:
        return
    doc = make_doc([("paragraph", text)])
    try:
        chunks = chunk_document(doc, REVISION, max_chunks=1_000_000, **p)
    except ProcessingError:  # pragma: no cover - cannot happen for non-empty normalized text
        raise
    check_invariants(doc, chunks, max_chars=p["max_chars"], revision=REVISION)
    assert chunks[0].locators[0].char_start == 0
    assert chunks[-1].locators[-1].char_end == len(text)
