"""Unit sanity of the memory evidence parsing and the prompt relevance (no database)."""

from __future__ import annotations

import uuid

from app.modules.ai.verification import content_tokens
from app.modules.memory import evidence as ev
from app.modules.memory.chat import _overlap


def test_stored_evidence_is_parsed_defensively() -> None:
    good = {"chunk_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4()),
            "version_id": str(uuid.uuid4()), "workspace_id": None}  # fmt: skip
    items = ev.parse_items([good, {"chunk_id": "nope"}, "x", {}])
    assert len(items) == 1
    assert items[0].to_json() == good
    assert ev.parse_items(None) == []
    assert ev.parse_items({"a": 1}) == []


def test_relevance_is_lexical_overlap_with_the_question() -> None:
    q = frozenset(content_tokens("What do otters eat at dusk?"))
    assert _overlap(q, "Otters eat crayfish at dusk") >= 0.5
    assert _overlap(q, "The quarterly invoice is overdue") == 0.0
