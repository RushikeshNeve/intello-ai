"""Approved memory as prompt evidence for chat (M07.01.02; contract m07 section 3).

Only ``approved``, not expired, not deleted memories that the run's creator may read (own, or
workspace-visible) AND whose evidence is ALL still ``ok`` for the run's scope are used; the best
few by lexical overlap with the question become pseudo-evidence items titled ``Memory (approved)``:
their text is the memory statement and they cite their first evidence chunk, so the existing
fence, ``verify`` and citation re-resolution apply unchanged (a memory whose document disappears
while the model writes loses its citation and is withheld like any other claim). Suggestions,
rejected, expired, invalidated and stale memories never reach the prompt. Memories without
evidence (preferences) are not cited evidence and are skipped.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.ai.verification import content_tokens
from app.modules.memory import evidence as ev
from app.modules.memory.models import STATE_APPROVED, Memory
from app.modules.retrieval.scope import RetrievalScope

MEMORY_TITLE: Final = "Memory (approved)"
MIN_OVERLAP: Final = 0.3
CANDIDATES: Final = 50


@dataclass(frozen=True)
class MemoryEvidence:
    """Structural ``EvidenceLike`` (see ``app.modules.ai.context``) for one approved memory."""

    evidence_id: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    text: str
    memory_id: uuid.UUID
    title: str = MEMORY_TITLE
    score: float = 0.0
    page_start: int | None = None
    page_end: int | None = None
    heading_path: tuple[str, ...] = ()


def _overlap(question_tokens: frozenset[str], statement: str) -> float:
    tokens = frozenset(content_tokens(statement))
    if not tokens or not question_tokens:
        return 0.0
    return len(tokens & question_tokens) / min(len(tokens), len(question_tokens))


def memory_evidence(
    session: Session,
    scope: RetrievalScope,
    question: str,
    base_evidence: Sequence[Any],
    *,
    limit: int,
) -> list[MemoryEvidence]:
    """Pseudo-evidence for up to ``limit`` relevant, valid, approved memories (can be empty)."""
    q_tokens = frozenset(content_tokens(question))
    if not q_tokens or limit < 1:
        return []
    rows = (
        session.execute(
            select(Memory)
            .where(
                Memory.org_id == scope.org_id,
                Memory.state == STATE_APPROVED,
                Memory.deleted_at.is_(None),
                or_(Memory.expires_at.is_(None), Memory.expires_at > func.now()),
            )
            .order_by(Memory.approved_at.desc().nulls_last(), Memory.id)
            .limit(CANDIDATES)
        )
        .scalars()
        .all()
    )
    present = {str(getattr(e, "chunk_id", "")) for e in base_evidence}
    scored: list[tuple[float, Memory, ev.EvidenceItem]] = []
    for row in rows:
        items = ev.parse_items(row.evidence)
        if not items:
            continue
        score = _overlap(q_tokens, row.statement)
        if score < MIN_OVERLAP:
            continue
        # Stale evidence (deleted, superseded, or outside this run's scope): skipped.
        states = ev.check(session, scope.org_id, items, scope.workspace_ids)
        if any(s.status != "ok" for s in states):
            continue
        if str(items[0].chunk_id) in present:
            continue  # the base evidence already carries that passage
        scored.append((score, row, items[0]))
    scored.sort(key=lambda t: (-t[0], str(t[1].id)))
    out: list[MemoryEvidence] = []
    for n, (_score, row, first) in enumerate(scored[:limit], start=1):
        out.append(
            MemoryEvidence(
                evidence_id=f"m{n}",
                chunk_id=first.chunk_id,
                document_id=first.document_id,
                version_id=first.version_id,
                text=row.statement,
                memory_id=row.id,
            )
        )
    return out
