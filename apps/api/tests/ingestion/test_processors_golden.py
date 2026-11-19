"""Golden extraction and chunking fixtures (M03.02.02 acceptance: golden fixtures pass).

Each input under ``tests/fixtures/ingestion/inputs`` is extracted and chunked; the result is
compared with ``golden/<name>.extracted.json`` and ``golden/<name>.chunks.json``. After an
*intended* behaviour change, regenerate with ``UPDATE_GOLDEN=1`` (and bump ``PIPELINE_VERSION``
in ``ids.py``):

    UPDATE_GOLDEN=1 python -m pytest tests/ingestion/test_processors_golden.py

then review the diff. ``golden/hashes.json`` pins the sha256 of each canonical byte string (the
value checkpoints fingerprint), so any drift of the canonical form is caught even if the JSON
still parses.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.modules.ingestion.chunking import chunk_document
from app.modules.ingestion.contracts import (
    Chunk,
    ExtractedDocument,
    canonical_json_bytes,
)
from app.modules.ingestion.processors import extract
from tests.ingestion.test_processors_support import (
    GOLDEN,
    GOLDEN_CHUNK_PARAMS,
    HTML,
    INPUTS,
    MARKDOWN,
    PDF,
    REVISION_ID,
    TEXT,
    block_network,
    check_golden,
    source_for,
    update_golden,
)

CASES = [
    ("sample_utf8.txt", TEXT),
    ("sample_latin1.txt", TEXT),
    ("sample_utf16le_bom.txt", TEXT),
    ("sample_utf16be_nobom.txt", TEXT),
    ("sample.md", MARKDOWN),
    ("sample.html", HTML),
    ("report.pdf", PDF),
    ("sentence_break.pdf", PDF),
    ("unicode.pdf", PDF),
]
HASHES = GOLDEN / "hashes.json"


def _run(name: str, content_type: str) -> tuple[ExtractedDocument, list[Chunk]]:
    data = (INPUTS / name).read_bytes()
    with block_network() as attempts:
        doc = extract(data, content_type, source=source_for(data, content_type))
        chunks = chunk_document(doc, REVISION_ID, **GOLDEN_CHUNK_PARAMS)
    assert attempts == []
    return doc, chunks


def _chunks_json(chunks: list[Chunk]) -> list[object]:
    return [json.loads(c.to_canonical_json()) for c in chunks]


@pytest.mark.parametrize(("name", "content_type"), CASES)
def test_extraction_matches_golden(name: str, content_type: str) -> None:
    doc, _ = _run(name, content_type)
    check_golden(GOLDEN / f"{name}.extracted.json", json.loads(doc.to_canonical_json()))
    if not update_golden():
        golden = ExtractedDocument.from_json((GOLDEN / f"{name}.extracted.json").read_bytes())
        assert golden == doc
        assert golden.to_canonical_json() == doc.to_canonical_json()


@pytest.mark.parametrize(("name", "content_type"), CASES)
def test_chunks_match_golden(name: str, content_type: str) -> None:
    doc, chunks = _run(name, content_type)
    check_golden(GOLDEN / f"{name}.chunks.json", _chunks_json(chunks))
    stream = doc.stream()
    for chunk in chunks:  # the offset invariant holds on every golden
        assert chunk.text == "\n\n".join(
            stream[ln.char_start : ln.char_end] for ln in chunk.locators
        )


def test_canonical_hashes_are_pinned() -> None:
    actual: dict[str, dict[str, str]] = {}
    for name, content_type in CASES:
        doc, chunks = _run(name, content_type)
        actual[name] = {
            "extracted_sha256": doc.sha256_hex(),
            "chunks_sha256": hashlib.sha256(
                canonical_json_bytes([json.loads(c.to_canonical_json()) for c in chunks])
            ).hexdigest(),
        }
    check_golden(HASHES, actual)


def test_extraction_is_deterministic() -> None:
    for name, content_type in CASES:
        first, first_chunks = _run(name, content_type)
        second, second_chunks = _run(name, content_type)
        assert first.to_canonical_json() == second.to_canonical_json()
        assert [c.to_canonical_json() for c in first_chunks] == [
            c.to_canonical_json() for c in second_chunks
        ]


def test_equivalent_encodings_extract_the_same_text() -> None:
    le, _ = _run("sample_utf16le_bom.txt", TEXT)
    be, _ = _run("sample_utf16be_nobom.txt", TEXT)
    assert [b.text for b in le.blocks] == [b.text for b in be.blocks]
    assert any("€" in b.text for b in le.blocks)  # the euro sign survives UTF-16


def test_latin1_text_is_decoded_to_the_right_characters() -> None:
    doc, _ = _run("sample_latin1.txt", TEXT)
    joined = doc.stream()
    for expected in ("marée", "très", "côte", "Straße", "Küste", "Fähre", "Último"):
        assert expected in joined
