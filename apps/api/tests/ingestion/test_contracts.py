"""The extracted-content contract (M03.02.01): modality, source locators, canonical JSON."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.modules.ingestion.contracts import (
    ERROR_CODES,
    Block,
    Chunk,
    ExtractedDocument,
    Locator,
    ProcessingError,
    ProcessorInfo,
    SourceInfo,
    Stats,
    canonical_json_bytes,
)

VERSION_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
SHA = "a" * 64


def _source() -> SourceInfo:
    return SourceInfo(version_id=VERSION_ID, sha256=SHA, content_type="text/plain", byte_size=11)


def _doc(*texts: str, modality: str = "text") -> ExtractedDocument:
    blocks = []
    pos = 0
    for i, text in enumerate(texts):
        kind = "page" if modality == "pdf" else ("offset" if modality == "html" else "line")
        blocks.append(
            Block(
                block_id=f"b{i}",
                kind="paragraph",
                text=text,
                locator=Locator(
                    kind=kind,  # type: ignore[arg-type]
                    page=1 if modality == "pdf" else None,
                    char_start=pos,
                    char_end=pos + len(text),
                    line_start=i + 1 if modality == "text" else None,
                    line_end=i + 1 if modality == "text" else None,
                ),
            )
        )
        pos += len(text) + 2
    return ExtractedDocument(
        modality=modality,  # type: ignore[arg-type]
        source=_source(),
        processor=ProcessorInfo(name="text", version="1"),
        blocks=blocks,
        stats=Stats(pages=1 if modality == "pdf" else None, chars=max(pos - 2, 0)),
    )


def test_document_carries_modality_and_source_locators() -> None:
    doc = _doc("hello", "world")
    assert doc.schema_version == 1
    assert doc.modality == "text"
    assert doc.source.version_id == VERSION_ID
    assert [b.block_id for b in doc.blocks] == ["b0", "b1"]
    assert doc.blocks[1].locator.char_start == 7  # "hello" + two-character separator
    assert doc.blocks[1].locator.line_start == 2
    assert doc.stream() == "hello\n\nworld"
    assert doc.stats.chars == len(doc.stream())


def test_canonical_json_is_pinned_and_byte_stable() -> None:
    doc = _doc("héllo", "world")
    raw = doc.to_canonical_json()
    assert raw == doc.to_canonical_json()
    assert b" " not in raw.replace("héllo".encode(), b"")  # no whitespace separators
    assert "héllo".encode() in raw  # UTF-8, not \\u escapes
    parsed = json.loads(raw)
    assert list(parsed) == sorted(parsed)  # sorted keys
    assert "language" not in parsed  # None fields are omitted
    # Pinned: a change here means every checkpoint fingerprint changes. Bump PIPELINE_VERSION.
    assert doc.sha256_hex() == hashlib.sha256(raw).hexdigest()
    assert doc.sha256_hex() == "6a47e2b226c9c4fe6abdc25042b7329e65a94dc6e956b8f9b89e7c45c169ee21"


def test_round_trip_through_json() -> None:
    doc = _doc("one", "two", "three")
    again = ExtractedDocument.from_json(doc.to_canonical_json())
    assert again == doc
    assert again.to_canonical_json() == doc.to_canonical_json()
    assert ExtractedDocument.from_json(doc.to_canonical_json().decode()) == doc


def test_canonical_json_rejects_nan_and_sorts_keys() -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json_bytes({"x": math.nan})
    assert canonical_json_bytes({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def _mutated(doc: ExtractedDocument, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(doc.to_canonical_json())
    data.update(changes)
    return data


def test_strict_parsing_rejects_unknown_fields_and_wrong_types() -> None:
    doc = _doc("hello")
    good = json.loads(doc.to_canonical_json())
    for bad in (
        {**good, "extra": 1},
        {**good, "schema_version": 2},
        {**good, "modality": "docx"},
        {**good, "blocks": "nope"},
        {**good, "source": {**good["source"], "byte_size": "11"}},
        {**good, "source": {**good["source"], "sha256": "XYZ"}},
        {**good, "source": {**good["source"], "extra": True}},
    ):
        with pytest.raises(ValidationError):
            ExtractedDocument.from_json(json.dumps(bad))
    with pytest.raises(ValidationError):
        ExtractedDocument.from_json(b"not json")
    with pytest.raises(ValidationError):
        ExtractedDocument.from_json(b'{"schema_version":1,"modality":"text",NaN:1}')


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["blocks"][0].update(block_id="b1"),  # ids must be b0, b1, ...
        lambda d: d["blocks"][0]["locator"].update(char_start=1),  # offsets follow the stream
        lambda d: d["blocks"][0]["locator"].update(char_end=99),  # span == text length
        lambda d: d["blocks"][0]["locator"].update(kind="page", page=1),  # wrong locator kind
        lambda d: d["blocks"][0].update(text="   "),  # blank block
        lambda d: d["blocks"][0].update(level=2),  # level only on headings
        lambda d: d["stats"].update(chars=3),  # stats must match the stream
        lambda d: d["stats"].update(pages=1),  # only pdf reports pages
        lambda d: d["blocks"][0]["locator"].update(line_end=0),  # lines are 1-based
    ],
)
def test_invariants_are_enforced(mutate: Any) -> None:
    data = json.loads(_doc("hello").to_canonical_json())
    mutate(data)
    with pytest.raises(ValidationError):
        ExtractedDocument.from_json(json.dumps(data))


def test_pdf_documents_need_pages_and_page_locators() -> None:
    doc = _doc("hello", modality="pdf")
    assert doc.blocks[0].locator.kind == "page"
    assert doc.blocks[0].locator.page == 1
    data = json.loads(doc.to_canonical_json())
    data["stats"].pop("pages")
    with pytest.raises(ValidationError):
        ExtractedDocument.from_json(json.dumps(data))
    data["stats"]["pages"] = 0  # a block on page 1 of a 0-page document
    with pytest.raises(ValidationError):
        ExtractedDocument.from_json(json.dumps(data))


def test_lone_surrogates_are_rejected_so_utf8_canonical_json_always_encodes() -> None:
    with pytest.raises(ValidationError):
        Block(
            block_id="b0",
            kind="paragraph",
            text="bad \ud800 text",
            locator=Locator(kind="line", char_start=0, char_end=10, line_start=1, line_end=1),
        )


def test_locator_validation() -> None:
    with pytest.raises(ValidationError):
        Locator(kind="page", char_start=0, char_end=1)  # page number missing
    with pytest.raises(ValidationError):
        Locator(kind="offset", char_start=5, char_end=1)
    with pytest.raises(ValidationError):
        Locator(kind="line", char_start=0, char_end=1, line_start=1)  # half a range
    with pytest.raises(ValidationError):
        Locator(kind="offset", char_start=0, char_end=1, section_path=["a"] * 33)
    ok = Locator(kind="page", page=3, char_start=0, char_end=1, section_path=["A", "B"])
    assert ok.section_path == ["A", "B"]


def test_models_are_immutable() -> None:
    doc = _doc("hello")
    with pytest.raises(ValidationError):
        doc.modality = "pdf"  # type: ignore[misc]


def test_processing_error_closed_code_set_and_flags() -> None:
    assert {
        "unsupported_type",
        "no_text_extracted",
        "pdf_unreadable",
        "encoding_unreadable",
        "too_large",
        "too_many_pages",
        "malformed_input",
        "ocr_unavailable",
        "transcription_unavailable",
        "video_unavailable",
    } == ERROR_CODES
    err = ProcessingError("unsupported_type", permanent=True)
    assert err.code == "unsupported_type"
    assert err.permanent is True
    assert err.to_dict() == {"code": "unsupported_type", "permanent": True, "detail": ""}
    with pytest.raises(ValueError, match="unknown processing error code"):
        ProcessingError("anything_else")


def test_processing_error_detail_is_bounded_single_line() -> None:
    err = ProcessingError("too_large", detail="line one\nline two " + "x" * 1000)
    assert "\n" not in err.detail
    assert len(err.detail) <= 200


def test_chunk_validators() -> None:
    text = "some chunk text"
    locator = Locator(kind="offset", char_start=0, char_end=len(text))
    good: dict[str, Any] = {
        "chunk_index": 0,
        "chunk_id": uuid.uuid4(),
        "text": text,
        "token_estimate": math.ceil(len(text) / 4),
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "locators": [locator],
        "heading_path": [],
    }
    chunk = Chunk(**good)
    assert (chunk.char_start, chunk.char_end) == (0, len(text))
    for bad in (
        {**good, "token_estimate": 1},
        {**good, "content_hash": "0" * 64},
        {**good, "text": "   "},
        {**good, "locators": []},
        {**good, "page_start": 2},
        {**good, "page_start": 3, "page_end": 2},
    ):
        with pytest.raises(ValidationError):
            Chunk(**bad)
