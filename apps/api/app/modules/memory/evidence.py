"""Memory evidence: validation at creation and re-validation at read/use time (contract m07 §3).

Evidence items are ``{chunk_id, document_id, version_id, workspace_id}``. A item is ``ok`` while its
document and workspace are live, its version is still the published one and the reader may read the
workspace; otherwise ``stale`` with a reason (``deleted``, ``superseded`` or ``no_access``). The
first two are facts about the evidence (they invalidate the memory for everybody); ``no_access`` is
only about the reader and never changes the memory.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.modules.retrieval.citations import CitationRef, resolve_citation
from app.modules.retrieval.scope import RetrievalScope

EvidenceStatus = Literal["ok", "stale"]
REASON_DELETED: Final = "deleted"
REASON_SUPERSEDED: Final = "superseded"
REASON_NO_ACCESS: Final = "no_access"
MAX_EVIDENCE_ITEMS: Final = 10

_DOC_SQL = text(
    "SELECT d.id AS document_id, d.workspace_id, d.title, d.deleted_at AS doc_deleted,"
    " w.deleted_at AS ws_deleted, p.version_id AS current_version_id"
    " FROM documents d JOIN workspaces w ON w.id = d.workspace_id AND w.org_id = d.org_id"
    " LEFT JOIN document_publications p ON p.document_id = d.id AND p.org_id = d.org_id"
    " WHERE d.org_id = :o AND d.id IN :ids"
).bindparams(bindparam("ids", expanding=True))


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    workspace_id: uuid.UUID | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "workspace_id": None if self.workspace_id is None else str(self.workspace_id),
        }


@dataclass(frozen=True, slots=True)
class EvidenceState:
    item: EvidenceItem
    status: EvidenceStatus
    reason: str | None  # None when ok
    title: str | None


def parse_items(raw: object) -> list[EvidenceItem]:
    """Stored evidence JSON -> items (malformed entries are skipped, never trusted)."""
    out: list[EvidenceItem] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        try:
            ws = entry.get("workspace_id")
            out.append(
                EvidenceItem(
                    chunk_id=uuid.UUID(str(entry["chunk_id"])),
                    document_id=uuid.UUID(str(entry["document_id"])),
                    version_id=uuid.UUID(str(entry["version_id"])),
                    workspace_id=None if ws is None else uuid.UUID(str(ws)),
                )
            )
        except (KeyError, ValueError):
            continue
    return out


def validate_for_creation(
    session: Session, scope: RetrievalScope, items: Sequence[EvidenceItem]
) -> list[EvidenceItem] | None:
    """Every item must resolve for the caller (live document, readable workspace, chunk of that
    version). Returns the items completed with their workspace, or ``None`` if any fails."""
    out: list[EvidenceItem] = []
    for item in items:
        found = resolve_citation(
            session,
            scope,
            CitationRef(
                chunk_id=item.chunk_id, document_id=item.document_id, version_id=item.version_id
            ),
        )
        if found is None:
            return None
        out.append(
            EvidenceItem(item.chunk_id, item.document_id, item.version_id, found.workspace_id)
        )
    return out


def check(
    session: Session,
    org_id: uuid.UUID,
    items: Iterable[EvidenceItem],
    readable: frozenset[uuid.UUID],
) -> list[EvidenceState]:
    """Current status of each item for a reader whose readable workspaces are ``readable``."""
    listed = list(items)
    if not listed:
        return []
    ids = list({i.document_id for i in listed})
    rows = {r.document_id: r for r in session.execute(_DOC_SQL, {"o": org_id, "ids": ids}).all()}
    out: list[EvidenceState] = []
    for item in listed:
        row: Any = rows.get(item.document_id)
        if row is None or row.doc_deleted is not None or row.ws_deleted is not None:
            out.append(EvidenceState(item, "stale", REASON_DELETED, None))
        elif row.current_version_id is not None and row.current_version_id != item.version_id:
            out.append(EvidenceState(item, "stale", REASON_SUPERSEDED, row.title))
        elif row.workspace_id not in readable:
            out.append(EvidenceState(item, "stale", REASON_NO_ACCESS, None))
        else:
            out.append(EvidenceState(item, "ok", None, row.title))
    return out


def invalidate_documents(
    session: Session, org_id: uuid.UUID, document_ids: Iterable[uuid.UUID], reason: str
) -> int:
    """Mark the org's live memories citing any of these documents ``invalidated`` (definer
    function: it crosses owners). Never raises into the caller's transaction."""
    ids = sorted({str(d) for d in document_ids})
    if not ids:
        return 0
    try:
        with session.begin_nested():
            count = session.execute(
                text("SELECT memory_invalidate_evidence(:o, CAST(:d AS uuid[]), :r)"),
                {"o": org_id, "d": ids, "r": reason},
            ).scalar_one()
    except Exception:  # noqa: BLE001 - invalidation is also done lazily and by the sweep
        return 0
    return int(count or 0)
