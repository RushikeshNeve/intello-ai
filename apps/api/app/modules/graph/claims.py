"""Claim + evidence-backed edge construction (M05.01.04, contract m05 sections 1.3 and 3).

A claim is ``verified`` only when at least one of its evidence chunks really exists in the
revision's chunks AND the chunk text lexically supports the claim's surface text (reuse of
``ai/verification.lexical_support``). Anything else is stored ``unverified`` and is never returned
by retrieval or traversal. An unresolvable chunk contributes no evidence at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from app.modules.ai.verification import lexical_support
from app.modules.graph.extract import EXTRACTOR_VERSION, ExtractedRelation, Extraction, SourceChunk
from app.modules.graph.ids import claim_id_for, evidence_id_for
from app.modules.graph.ontology import UNVERIFIED, VERIFIED, normalize_name
from app.modules.graph.resolve import Resolved
from app.modules.knowledge.ports import ClaimRecord, EvidenceRecord

SUPPORT_MIN: Final = 0.8
EXCERPT_CHARS: Final = 300


@dataclass(slots=True)
class BuiltClaims:
    claims: list[ClaimRecord] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)

    @property
    def verified(self) -> int:
        return sum(1 for c in self.claims if c.verification == VERIFIED)


def _excerpt(text: str, surface: str) -> str:
    at = text.casefold().find(surface.casefold())
    start = max(0, at - 100) if at >= 0 else 0
    return text[start : start + EXCERPT_CHARS]


def build_claims(
    extraction: Extraction,
    resolved: Resolved,
    chunks: Sequence[SourceChunk],
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    extractor_version: str = EXTRACTOR_VERSION,
) -> BuiltClaims:
    by_chunk = {c.chunk_id: c for c in chunks if c.revision_id == revision_id}
    groups: dict[tuple[uuid.UUID, str, str], list[ExtractedRelation]] = {}
    for rel in extraction.relations:
        subject = resolved.ids.get(rel.subject)
        if subject is None:
            continue
        if rel.object is not None:
            obj = resolved.ids.get(rel.object)
            if obj is None or obj == subject:
                continue
            key = (subject, rel.predicate, str(obj))
        else:
            key = (subject, rel.predicate, f"text:{normalize_name(rel.object_text or '')}")
        groups.setdefault(key, []).append(rel)

    built = BuiltClaims()
    for (subject, predicate, object_key), rels in sorted(groups.items(), key=lambda kv: str(kv[0])):
        claim_id = claim_id_for(subject, predicate, object_key, revision_id)
        object_id = None if object_key.startswith("text:") else uuid.UUID(object_key)
        object_text = rels[0].object_text if object_id is None else None
        best = 0.0
        supported: list[EvidenceRecord] = []
        existing: list[EvidenceRecord] = []
        seen: set[uuid.UUID] = set()
        for rel in rels:
            chunk = by_chunk.get(rel.chunk_id)
            if chunk is None or chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            support = lexical_support(f"{rel.subject_surface} {rel.object_surface}", chunk.text)
            best = max(best, support)
            ev = EvidenceRecord(
                id=evidence_id_for(claim_id, chunk.chunk_id),
                claim_id=claim_id,
                org_id=org_id,
                workspace_id=workspace_id,
                chunk_id=chunk.chunk_id,
                revision_id=revision_id,
                document_id=document_id,
                version_id=chunk.version_id,
                excerpt=_excerpt(chunk.text, rel.subject_surface),
            )
            existing.append(ev)
            if support >= SUPPORT_MIN:
                supported.append(ev)
        verified = bool(supported)
        built.claims.append(
            ClaimRecord(
                id=claim_id,
                org_id=org_id,
                workspace_id=workspace_id,
                document_id=document_id,
                revision_id=revision_id,
                subject_id=subject,
                predicate=predicate,
                object_id=object_id,
                object_text=object_text,
                confidence=round(best, 3),
                verification=VERIFIED if verified else UNVERIFIED,
                extractor_version=extractor_version,
            )
        )
        built.evidence.extend(supported if verified else existing)
    return built
