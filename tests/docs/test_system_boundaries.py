"""M00.01.01 acceptance: boundary doc ADR references are consistent with the ADR register."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "plan" / "ADR_REGISTER.md"
BOUNDARIES = ROOT / "docs" / "architecture" / "system-boundaries.md"

ROW = re.compile(r"^\|\s*(ADR-\d{3})\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
REF = re.compile(r"ADR-\d{3}")


def _register_rows(text: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        m = ROW.match(line)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3))
    return rows


def _index_section(text: str) -> str:
    start = text.index("## 5. ADR index")
    end = text.index("## 6.", start)
    return text[start:end]


def test_register_has_57_sequential_adrs() -> None:
    rows = _register_rows(REGISTER.read_text(encoding="utf-8"))
    assert sorted(rows) == [f"ADR-{i:03d}" for i in range(1, 58)]


def test_every_referenced_adr_exists() -> None:
    known = set(_register_rows(REGISTER.read_text(encoding="utf-8")))
    referenced = set(REF.findall(BOUNDARIES.read_text(encoding="utf-8")))
    unknown = referenced - known
    assert not unknown, f"unknown ADR references: {sorted(unknown)}"


def test_index_matches_register_exactly() -> None:
    register = _register_rows(REGISTER.read_text(encoding="utf-8"))
    index = _register_rows(_index_section(BOUNDARIES.read_text(encoding="utf-8")))
    assert index == register


def test_core_ownership_boundaries_present() -> None:
    text = BOUNDARIES.read_text(encoding="utf-8")
    for needle in (
        "| Users, organizations, workspaces, memberships, roles, grants | PostgreSQL",
        "| Original document bytes, immutable versions | Object store",
        "| Chunks, embeddings, entities, claims, relationships | RushDB",
        "| Ingestion jobs, stage checkpoints, outbox, sync cursors | PostgreSQL",
    ):
        assert needle in text, needle
