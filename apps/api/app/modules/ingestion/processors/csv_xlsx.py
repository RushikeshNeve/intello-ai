"""CSV and XLSX processors (M06.02.01): row-group blocks with ``cell`` locators.

Each block is the (first non-empty) header row of its sheet followed by up to ``ROWS_PER_BLOCK``
data rows, cells joined by `` | ``. The locator names the sheet and the 1-based row/column range
of the DATA rows in the block (the header is repeated for retrieval quality, it is row ``header``
of the sheet and not part of the range). Citations therefore read "Sheet1 rows 12-31, columns 1-4".

Safety: XLSX is read with openpyxl ``read_only`` + ``data_only`` (cached values only: formulas are
never evaluated, external links are not loaded, no macros run). The zip is pre-checked for entry
count and total uncompressed size (zip-bomb guard). Caps (``MAX_SHEETS``/``MAX_ROWS``/``MAX_CELLS``)
TRUNCATE the extraction, they do not fail it; hidden sheets are skipped. CSV is a text format:
delimiter sniffed among ``, ; tab |`` and the sheet name is ``CSV``.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable
from datetime import date, datetime, time
from typing import Any, Final

from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError, SourceInfo
from app.modules.ingestion.processors.base import DocumentBuilder, Limits, check_input_size
from app.modules.ingestion.processors.decoding import charset_from_content_type, decode_bytes

MAX_SHEETS: Final = 50
MAX_ROWS_PER_SHEET: Final = 100_000
MAX_CELLS: Final = 1_000_000
ROWS_PER_BLOCK: Final = 25
MAX_BLOCK_CHARS: Final = 3000
MAX_CELL_CHARS: Final = 1000
MAX_ZIP_ENTRIES: Final = 5000
MAX_UNCOMPRESSED_BYTES: Final = 200 * 1024 * 1024
CSV_SHEET: Final = "CSV"

Row = tuple[int, list[str]]  # (1-based row number, cell texts)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())[:MAX_CELL_CHARS]


def _emit_sheet(
    builder: DocumentBuilder, sheet: str, rows: Iterable[Row], budget: list[int]
) -> None:
    """Group non-empty rows into blocks; ``budget`` is the shared remaining cell count."""
    header: list[str] | None = None
    header_row = 1
    emitted = False
    group: list[Row] = []
    chars = 0

    def flush() -> None:
        nonlocal chars, emitted
        if not group or header is None:
            group.clear()
            chars = 0
            return
        used = [i for _, r in group for i, c in enumerate(r, 1) if c]
        lines = [" | ".join(header)] + [" | ".join(r) for _, r in group]
        builder.add(
            "table",
            "\n".join(lines),
            section_path=(sheet,),
            extra={
                "sheet": sheet[:500],
                "row_start": group[0][0],
                "row_end": group[-1][0],
                "col_start": min(used),
                "col_end": max(used),
            },
        )
        emitted = True
        group.clear()
        chars = 0

    for number, cells in rows:
        if number > MAX_ROWS_PER_SHEET or budget[0] <= 0:
            break
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        budget[0] -= len(cells)
        if header is None:
            header, header_row = cells, number
            continue
        line_chars = sum(map(len, cells)) + 3 * len(cells)
        if group and (len(group) >= ROWS_PER_BLOCK or chars + line_chars > MAX_BLOCK_CHARS):
            flush()
        group.append((number, cells))
        chars += line_chars
    flush()
    if header is not None and not emitted:  # a header-only sheet still carries its text
        builder.add(
            "table",
            " | ".join(header),
            section_path=(sheet,),
            extra={
                "sheet": sheet[:500],
                "row_start": header_row,
                "row_end": header_row,
                "col_start": 1,
                "col_end": len(header),
            },
        )


def extract_csv(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    check_input_size(data, limits)
    if not data.strip():
        raise ProcessingError("no_text_extracted", detail="empty input")
    text = decode_bytes(data, hint=charset_from_content_type(content_type))
    try:
        dialect: Any = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    csv.field_size_limit(1_000_000)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), dialect)
        rows = ((n, [_cell_text(c) for c in cells]) for n, cells in enumerate(reader, 1))
        builder = DocumentBuilder("spreadsheet", source, "csv", limits)
        _emit_sheet(builder, CSV_SHEET, rows, [MAX_CELLS])
    except csv.Error as exc:
        raise ProcessingError("malformed_input", detail="unreadable CSV") from exc
    return builder.build()


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ProcessingError("malformed_input", detail="not a valid xlsx archive") from exc
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise ProcessingError("too_large", detail="xlsx archive expands too much")


def extract_xlsx(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    check_input_size(data, limits)
    if not data:
        raise ProcessingError("no_text_extracted", detail="empty input")
    _check_zip(data)
    from openpyxl import load_workbook  # imported lazily: only xlsx jobs pay for it

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        raise ProcessingError(
            "malformed_input", detail=f"cannot parse: {type(exc).__name__}"
        ) from exc
    builder = DocumentBuilder("spreadsheet", source, "xlsx", limits)
    budget = [MAX_CELLS]
    try:
        sheets = [ws for ws in workbook.worksheets if ws.sheet_state == "visible"][:MAX_SHEETS]
        for ws in sheets:
            try:
                ws.reset_dimensions()
                rows = (
                    (n, [_cell_text(c) for c in cells])
                    for n, cells in enumerate(ws.iter_rows(values_only=True), 1)
                )
                _emit_sheet(builder, ws.title, rows, budget)
            except ProcessingError:
                raise
            except Exception as exc:
                raise ProcessingError(
                    "malformed_input", detail=f"sheet unreadable: {type(exc).__name__}"
                ) from exc
    finally:
        workbook.close()
    return builder.build()
