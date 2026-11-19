# ruff: noqa: PT018, PT011
"""M06.02 sanity tests: CSV/XLSX cell locators, code line locators, OCR/audio/video capabilities."""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator

import pytest
from openpyxl import Workbook

from app.modules.ingestion.chunking import chunk_document
from app.modules.ingestion.contracts import (
    UNSUPPORTED_CODES,
    ExtractedDocument,
    Locator,
    ProcessingError,
)
from app.modules.ingestion.pipeline import UNSUPPORTED_CODES as PIPELINE_CODES
from app.modules.ingestion.processors.capabilities import (
    FormatSettings,
    OcrResult,
    TranscriptResult,
    TranscriptSegment,
    reset_engines,
    set_engines_for_tests,
)
from app.modules.ingestion.processors.code import (
    content_type_for_filename,
    repository_locator,
    repository_metadata,
)
from app.modules.ingestion.processors.registry import XLSX_CONTENT_TYPE
from app.modules.ingestion.schemas import ERROR_MESSAGES
from tests.ingestion.test_processors_support import run

REVISION = uuid.UUID("44444444-4444-4444-8444-444444444444")


@pytest.fixture(autouse=True)
def _engines() -> Iterator[None]:
    yield
    reset_engines()


def _xlsx(build) -> bytes:  # type: ignore[no-untyped-def]
    wb = Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _chunk_locators(doc: ExtractedDocument) -> list[dict[str, object]]:
    return [
        loc.model_dump(mode="json", exclude_none=True)
        for chunk in chunk_document(doc, REVISION)
        for loc in chunk.locators
    ]


def test_csv_cell_locators_survive_chunking_and_point_at_real_rows() -> None:
    rows = ["id,name,qty"] + [f"{i},item{i},{i * 2}" for i in range(1, 61)]
    doc = run("\n".join(rows).encode(), "text/csv")
    assert doc.modality == "spreadsheet"
    first = doc.blocks[0]
    assert first.text.startswith("id | name | qty\n")  # header repeated per block
    assert doc.blocks[1].text.startswith("id | name | qty\n")
    loc = first.locator
    assert (loc.kind, loc.sheet, loc.row_start, loc.row_end) == ("cell", "CSV", 2, 26)
    assert (loc.col_start, loc.col_end) == (1, 3)
    assert "25 | item25 | 50" in first.text and "item26" not in first.text
    dumped = _chunk_locators(doc)
    assert all(d["kind"] == "cell" and d["sheet"] == "CSV" for d in dumped)
    assert {"row_start", "row_end", "col_start", "col_end"} <= dumped[0].keys()
    # a locator survives a strict JSON round trip (artifact store -> evidence endpoint)
    assert Locator.from_json(Locator.model_validate(dumped[0]).to_canonical_json())


def test_xlsx_sheets_formulas_links_and_hidden_sheets() -> None:
    def build(wb: Workbook) -> None:
        ws = wb.active
        ws.title = "Sales"
        ws.append(["region", "total"])
        ws.append(["north", 10])
        ws.append(["south", 32])
        ws["B4"] = "=SUM(B2:B3)"
        ws["C2"] = '=HYPERLINK("http://evil.example/x","click")'
        other = wb.create_sheet("Notes")
        other.append(["note"])
        other.append(["hello world"])
        hidden = wb.create_sheet("Secret")
        hidden.append(["secret"])
        hidden.append(["do not index"])
        hidden.sheet_state = "hidden"

    doc = run(_xlsx(build), XLSX_CONTENT_TYPE)
    text = doc.stream()
    assert "north | 10" in text and "hello world" in text
    assert "42" not in text  # the SUM formula was not evaluated
    assert "evil.example" not in text and "do not index" not in text
    by_sheet = {b.locator.sheet: b.locator for b in doc.blocks}
    assert set(by_sheet) == {"Sales", "Notes"}
    assert (by_sheet["Sales"].row_start, by_sheet["Sales"].row_end) == (2, 3)
    assert by_sheet["Sales"].section_path == ["Sales"]
    assert {d["sheet"] for d in _chunk_locators(doc)} == {"Sales", "Notes"}


def test_xlsx_row_caps_truncate_and_garbage_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ingestion.processors import csv_xlsx

    monkeypatch.setattr(csv_xlsx, "MAX_ROWS_PER_SHEET", 30)

    def build(wb: Workbook) -> None:
        ws = wb.active
        ws.append(["a"])
        for i in range(100):
            ws.append([f"v{i}"])

    doc = run(_xlsx(build), XLSX_CONTENT_TYPE)
    assert max(b.locator.row_end or 0 for b in doc.blocks) == 30
    with pytest.raises(ProcessingError) as err:
        run(b"not a zip", XLSX_CONTENT_TYPE)
    assert err.value.code == "malformed_input"


def test_code_line_locators_and_repository_pin() -> None:
    src = (
        "import os\n\n\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n\nclass C:\n    pass\n"
    )
    doc = run(src.encode(), content_type_for_filename("pkg/mod.py") or "")
    assert doc.modality == "code" and doc.language == "python"
    spans = [(b.locator.line_start, b.locator.line_end) for b in doc.blocks]
    assert spans == [(1, 1), (4, 5), (8, 9), (11, 12)]
    assert doc.blocks[1].text == "def a():\n    return 1"  # indentation kept
    assert all(d["kind"] == "line" for d in _chunk_locators(doc))
    sha = "a" * 40
    meta = repository_metadata("acme/widgets", "pkg/mod.py", sha)
    assert repository_locator(meta, 4) == f"acme/widgets@{sha}:pkg/mod.py#L4"
    with pytest.raises(ValueError):
        repository_metadata("acme/widgets", "pkg/mod.py", "main")  # a moving ref is not a pin


def test_ocr_disabled_is_unsupported_and_fake_engine_records_provenance() -> None:
    with pytest.raises(ProcessingError) as err:
        run(b"\x89PNG....", "image/png")
    assert err.value.code == "ocr_unavailable" and err.value.permanent

    class Fake:
        def recognize(self, data: bytes, content_type: str) -> OcrResult:
            return OcrResult("Invoice 42\n\nTotal due: 10", "fakeocr", "2.1", confidence=0.93)

    set_engines_for_tests(ocr=Fake(), settings=FormatSettings(OCR_ENABLED=True))
    doc = run(b"\x89PNG....", "image/png")
    assert (doc.processor.name, doc.processor.version) == ("ocr-fakeocr", "2.1")
    assert doc.stats.confidence == 0.93 and len(doc.blocks) == 2
    assert doc.blocks[0].locator.kind == "offset"


def test_audio_transcript_time_locators_limits_and_disabled() -> None:
    with pytest.raises(ProcessingError) as err:
        run(b"ID3", "audio/mpeg")
    assert err.value.code == "transcription_unavailable"

    class Fake:
        def transcribe(self, data: bytes, content_type: str) -> TranscriptResult:
            segs = [TranscriptSegment(i * 10.0, i * 10.0 + 9.5, f"sentence {i}.") for i in range(9)]
            return TranscriptResult(segs, "fakestt", "1", duration=95.0, language="en")

    cfg = FormatSettings(TRANSCRIBE_ENABLED=True, TRANSCRIBE_MAX_BYTES=100)
    set_engines_for_tests(transcription=Fake(), settings=cfg)
    doc = run(b"ID3" * 10, "audio/mpeg")
    assert doc.modality == "audio" and doc.language == "en" and doc.stats.duration_seconds == 95.0
    loc = doc.blocks[0].locator
    assert (loc.kind, loc.time_start, loc.time_end) == (
        "time",
        0.0,
        29.5,
    )  # 3 segments per 30 s window
    assert "sentence 0." in doc.blocks[0].text and len(doc.blocks) == 3
    dumped = _chunk_locators(doc)
    assert dumped[0]["time_start"] == 0.0
    with pytest.raises(ProcessingError) as big:  # size limit is checked before the engine runs
        run(b"x" * 101, "audio/mpeg")
    assert big.value.code == "too_large"
    set_engines_for_tests(
        transcription=Fake(),
        settings=FormatSettings(TRANSCRIBE_ENABLED=True, TRANSCRIBE_MAX_SECONDS=60),
    )
    with pytest.raises(ProcessingError) as long:
        run(b"ID3", "audio/mpeg")
    assert long.value.code == "too_large"


def test_video_is_explicitly_unsupported_and_pipeline_maps_codes() -> None:
    with pytest.raises(ProcessingError) as err:
        run(b"\x00\x00\x00 ftyp", "video/mp4")
    assert err.value.code == "video_unavailable"
    assert UNSUPPORTED_CODES is PIPELINE_CODES
    for code in ("ocr_unavailable", "transcription_unavailable", "video_unavailable"):
        assert code in UNSUPPORTED_CODES and code in ERROR_MESSAGES


def test_capabilities_matrix_reflects_settings() -> None:
    from app.modules.ingestion.capabilities_router import get_capabilities

    off = get_capabilities(None)  # type: ignore[arg-type]
    assert (off.ocr, off.transcription, off.video) == (False, False, False)
    assert "text/csv" in off.formats and "image/png" not in off.formats
    assert "video/mp4" not in off.formats and "audio/mpeg" not in off.formats

    class Fake:
        def recognize(self, data: bytes, content_type: str) -> OcrResult:
            return OcrResult("x", "f", "1")

    set_engines_for_tests(ocr=Fake(), settings=FormatSettings(OCR_ENABLED=True))
    on = get_capabilities(None)  # type: ignore[arg-type]
    assert on.ocr is True and "image/png" in on.formats
