"""Regenerates the binary fixture inputs (encoded text files and PDFs). Run by hand, not by tests.

    scripts/dev/linux-runner/run.sh apps/api "apt-get install -y -q fonts-dejavu-core \
      && python tests/fixtures/ingestion/generate_inputs.py"

All text is original. The PDFs are made with fpdf2 (dev dependency) with a fixed creation date,
so the output is reproducible for a given fpdf2 version; the committed bytes are the source of
truth for the golden files (``test_processors_golden.py``). The Unicode PDF needs a TrueType font:
DejaVu Sans
(free licence, embeddable) from the Debian ``fonts-dejavu-core`` package.
"""

from __future__ import annotations

import codecs
import os
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs"
FONT = Path(os.environ.get("UKOS_TTF", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))

LATIN1_TEXT = (
    "Journal du phare\n\n"
    "Le gardien a noté que la marée était très haute ce matin. Les bateaux sont rentrés tôt, "
    "et le café était déjà prêt. À midi, une tempête s'est levée sur la côte.\n\n"
    "Nachtrag auf Deutsch: Die Straße zum Hafen ist wegen des Sturms gesperrt. "
    "Über der Küste hängen schwere Wolken, und die Fähre fährt heute nicht.\n\n"
    "Último apunte: el faro funciona con normalidad; la niebla es espesa en la bahía.\n"
)
UTF16_TEXT = (
    "Fischerei-Protokoll\r\n\r\n"
    "Dieses Dokument ist in UTF-16 mit Byte-Order-Mark gespeichert.\r\n"
    "Zweite Zeile mit Umlauten: äöü und dem Zeichen €.\r\n\r\n"
    "Ende des Protokolls.\r\n"
)


def _pdf(**kw: object) -> FPDF:
    pdf = FPDF(**kw)  # type: ignore[arg-type]
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))
    pdf.set_title("fixture")
    pdf.set_author("fixture")
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=False)
    return pdf


def report_pdf() -> bytes:
    """Three pages, headings (larger bold font), wrapped paragraphs."""
    pdf = _pdf()
    pages = [
        (
            "Annual Orchard Report",
            [
                "This report summarises the orchard season. Apples ripened early this year, "
                "and the pear harvest followed in the first week of autumn.",
                "Volunteers helped with picking. Their effort doubled the amount of fruit "
                "collected per day compared with the previous season.",
            ],
        ),
        (
            "Findings",
            [
                "Soil moisture stayed within the target range for most of the summer. "
                "Pest pressure was low and no chemical treatment was needed.",
                "The east block produced the highest yield. The west block lagged because of "
                "late frost damage in spring.",
            ],
        ),
        (
            "Recommendations",
            [
                "Plant frost-tolerant varieties in the west block. Extend the volunteer "
                "programme and schedule picking days earlier in the calendar."
            ],
        ),
    ]
    for heading, paragraphs in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 18)
        pdf.multi_cell(0, 10, heading, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_font("Helvetica", "", 11)
        for paragraph in paragraphs:
            pdf.multi_cell(0, 6, paragraph, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)
    return bytes(pdf.output())


def sentence_break_pdf() -> bytes:
    """A sentence that is broken by a page break."""
    pdf = _pdf()
    pdf.set_font("Helvetica", "", 11)
    pdf.add_page()
    pdf.multi_cell(
        0,
        6,
        "Minutes of the committee meeting. The members reviewed the budget for the coming "
        "year and, after a long discussion about the cost of the new library roof, the "
        "committee decided to",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.add_page()
    pdf.multi_cell(
        0,
        6,
        "postpone the vote until the next meeting. The chair thanked everyone for attending.",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    return bytes(pdf.output())


def unicode_pdf() -> bytes:
    """Greek, Cyrillic and accented Latin text through an embedded TrueType font."""
    pdf = _pdf()
    pdf.add_font("DejaVu", "", str(FONT))
    pdf.set_font("DejaVu", "", 12)
    pdf.add_page()
    pdf.multi_cell(0, 7, "Unicode sample", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.multi_cell(
        0, 7, "Greek: Γεια σου κόσμε. "
        "Η θάλασσα είναι "
        "γαλήνια.",
        new_x="LMARGIN", new_y="NEXT",
    )  # fmt: skip
    pdf.ln(3)
    pdf.multi_cell(
        0, 7, "Cyrillic: Привет, мир! "
        "Море спокойно.",
        new_x="LMARGIN", new_y="NEXT",
    )  # fmt: skip
    pdf.ln(3)
    pdf.multi_cell(
        0, 7, "Latin: café naïve façade Łódź Ångström.",
        new_x="LMARGIN", new_y="NEXT",
    )  # fmt: skip
    return bytes(pdf.output())


def main() -> None:
    INPUTS.mkdir(parents=True, exist_ok=True)
    (INPUTS / "sample_latin1.txt").write_bytes(LATIN1_TEXT.encode("latin-1"))
    (INPUTS / "sample_utf16le_bom.txt").write_bytes(
        codecs.BOM_UTF16_LE + UTF16_TEXT.encode("utf-16-le")
    )
    (INPUTS / "sample_utf16be_nobom.txt").write_bytes(UTF16_TEXT.encode("utf-16-be"))
    (INPUTS / "report.pdf").write_bytes(report_pdf())
    (INPUTS / "sentence_break.pdf").write_bytes(sentence_break_pdf())
    (INPUTS / "unicode.pdf").write_bytes(unicode_pdf())


if __name__ == "__main__":
    main()
