"""A tiny deterministic PDF writer for the seed corpus (no dependencies, Helvetica, ASCII text).

``python -m tests.evaluation.pdfwriter`` style use is not needed: ``make_pdfs.py`` calls
``build_pdf`` and writes ``corpus/files/*.pdf``; a test checks the committed bytes are reproducible.
"""

from __future__ import annotations

import textwrap

Page = list[tuple[str, int, str]]  # (kind "h"|"p", font size, text)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page_stream(page: Page) -> bytes:
    ops = ["BT"]
    y = 790
    for kind, size, text in page:
        font = "/F2" if kind == "h" else "/F1"
        width = 60 if kind == "h" else 90
        for line in textwrap.wrap(text, width=width) or [""]:
            y -= size + 6
            ops.append(f"{font} {size} Tf 1 0 0 1 56 {y} Tm ({_escape(line)}) Tj")
        y -= 10 if kind == "p" else 6
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def build_pdf(pages: list[Page]) -> bytes:
    """Return a valid multi-page PDF. Same input, same bytes."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # pages, filled below
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # 3
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")  # 4
    kids: list[int] = []
    for page in pages:
        stream = _page_stream(page)
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_id = add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content} 0 R "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>".encode()
        )
        kids.append(page_id)
    objects[1] = (
        f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>"
    ).encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


PDF_SOURCES: dict[str, list[Page]] = {
    "eng-security.pdf": [
        [
            ("h", 18, "Security baseline"),
            (
                "p",
                11,
                "Passwords must have at least 14 characters. Multi-factor authentication is "
                "mandatory for all staff accounts. Access keys rotate every 90 days.",
            ),
        ],
        [
            ("h", 18, "Incident severity"),
            (
                "p",
                11,
                "Severity 1 incidents, where customer data is at risk, must be reported to the "
                "security officer within 1 hour. Severity 2 incidents must be reported within "
                "24 hours.",
            ),
        ],
    ],
    "hr-benefits.pdf": [
        [
            ("h", 18, "Employee benefits"),
            (
                "p",
                11,
                "The company pension plan matches employee contributions up to 6 percent of "
                "gross salary. Health insurance covers the employee and dependants from the "
                "first day of employment.",
            ),
        ],
        [
            ("h", 18, "Learning and wellbeing"),
            (
                "p",
                11,
                "Each employee has a learning budget of 1,200 EUR per year. A gym membership "
                "is reimbursed up to 40 EUR per month.",
            ),
        ],
    ],
    "sales-q3-report.pdf": [
        [
            ("h", 18, "Q3 2026 sales report"),
            (
                "p",
                11,
                "Revenue in Q3 2026 was 4.82 million EUR, up 12 percent on Q2. The team won "
                "37 new logos and the win rate was 27 percent.",
            ),
        ],
        [
            ("h", 18, "Pipeline"),
            (
                "p",
                11,
                "The open pipeline at the end of September was 11.3 million EUR. The largest "
                "deal is Harborline Logistics at 640,000 EUR, expected to close on 2026-12-15.",
            ),
        ],
    ],
}
