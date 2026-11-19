"""Regenerate the seed-corpus PDFs: ``python tests/evaluation/make_pdfs.py`` (run by hand)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pdfwriter import PDF_SOURCES, build_pdf


def main() -> None:
    target = HERE / "corpus" / "files"
    for name, pages in PDF_SOURCES.items():
        (target / name).write_bytes(build_pdf(pages))
        print("wrote", name)


if __name__ == "__main__":
    main()
