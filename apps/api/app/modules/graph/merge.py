"""Graph-enhanced evidence merge (M05.02.03, contract m05 §4).

Graph paths only SELECT claims; what reaches the prompt are the ORIGINAL chunks that support
them, loaded through the ``ChunkSource`` and revalidated against PostgreSQL by
``retrieval.revalidate`` (deleted/superseded/out-of-scope evidence is dropped). The extra items
are :class:`GraphEvidence` (an ``Evidence`` tagged ``via_graph`` with a short path text), so the
chat runtime consumes them like normal evidence and citations point at chunks, never at nodes.

Any graph failure degrades to the base evidence (the document search must keep working).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from typing import Final

from sqlalchemy.orm import Session

from app.modules.graph.ports_read import (
    ChunkRef,
    ChunkSource,
    GClaim,
    GEvidence,
    GraphReadRepository,
)
from app.modules.graph.read_settings import GraphReadSettings, get_graph_read_settings
from app.modules.graph.router import RouterMode, route
from app.modules.graph.traversal import Subgraph, traverse
from app.modules.retrieval.evidence import Evidence, revalidate
from app.modules.retrieval.ranking import RRF_K, Candidate
from app.modules.retrieval.scope import RetrievalScope

log = logging.getLogger(__name__)

MAX_PER_CLAIM: Final = 2


@dataclass(frozen=True, slots=True)
class GraphEvidence(Evidence):
    """Revalidated evidence found through the graph (built from a revalidated ``Evidence``)."""

    via_graph: bool = True
    path: str = ""


def resolve_evidence(
    session: Session,
    scope: RetrievalScope,
    claims: Sequence[GClaim],
    repo: GraphReadRepository,
    chunks: ChunkSource,
) -> dict[uuid.UUID, list[tuple[GEvidence, Evidence]]]:
    """Per claim id: its evidence that survives chunk loading AND PostgreSQL revalidation."""
    by_claim = {c.id: c for c in claims}
    graph_ev = repo.get_evidence(scope, list(by_claim))
    refs: dict[uuid.UUID, ChunkRef] = {}
    for ev in graph_ev:
        claim = by_claim.get(ev.claim_id)
        if claim is not None and ev.chunk_id not in refs:
            refs[ev.chunk_id] = ChunkRef(
                ev.chunk_id, ev.document_id, ev.version_id, ev.revision_id, claim.workspace_id
            )
    hits = chunks.fetch_chunks(session, scope, list(refs.values()))
    cands = [Candidate(h, 1.0 / (RRF_K + i + 1)) for i, h in enumerate(hits) if h.chunk_id in refs]
    alive, _dropped = revalidate(session, scope, cands)
    live = {e.chunk_id: e for e in alive}
    out: dict[uuid.UUID, list[tuple[GEvidence, Evidence]]] = {}
    for ev in graph_ev:
        e = live.get(ev.chunk_id)
        if e is not None and ev.claim_id in by_claim:
            out.setdefault(ev.claim_id, []).append((ev, e))
    return out


def describe_path(sub: Subgraph, claim: GClaim) -> str:
    def name(i: uuid.UUID | None) -> str:
        e = sub.nodes.get(i) if i is not None else None
        return e.canonical_name if e is not None else "?"

    return f"{name(claim.subject_id)} -[{claim.predicate}]-> {name(claim.object_id)}"


def augment_evidence(
    session: Session,
    scope: RetrievalScope,
    question: str,
    base_evidence: Sequence[Evidence],
    repo: GraphReadRepository,
    chunks: ChunkSource,
    *,
    mode: RouterMode | None = None,
    settings: GraphReadSettings | None = None,
) -> list[Evidence]:
    """``base_evidence`` followed by graph-found original chunks (never duplicating a base chunk).

    The caller gates this behind ``GRAPH_ENABLED``. Basic questions return the base unchanged
    without any graph call.
    """
    base = list(base_evidence)
    cfg = settings or get_graph_read_settings()
    try:
        decision = route(scope, repo, question, mode=mode or cfg.GRAPH_ROUTER_MODE)
        if decision.route == "basic" or cfg.GRAPH_MAX_EVIDENCE_EXTRA == 0:
            return base
        sub = traverse(
            scope,
            repo,
            decision.seeds,
            depth=cfg.GRAPH_TRAVERSAL_MAX_DEPTH,
            max_nodes=cfg.GRAPH_MAX_NODES,
        )
        ordered = sorted(sub.edges, key=lambda c: (sub.hop[c.id], -c.confidence, str(c.id)))
        resolved = resolve_evidence(session, scope, ordered, repo, chunks)
        have = {e.chunk_id for e in base}
        extra: list[Evidence] = []
        for claim in ordered:
            taken = 0
            for _gev, ev in resolved.get(claim.id, []):
                if ev.chunk_id in have or taken >= MAX_PER_CLAIM:
                    continue
                if len(extra) >= cfg.GRAPH_MAX_EVIDENCE_EXTRA:
                    break
                have.add(ev.chunk_id)
                taken += 1
                tagged = GraphEvidence(
                    **{f.name: getattr(ev, f.name) for f in fields(Evidence)},
                    via_graph=True,
                    path=describe_path(sub, claim),
                )
                extra.append(replace(tagged, evidence_id=f"e{len(base) + len(extra) + 1}"))
        return base + extra
    except Exception as exc:  # noqa: BLE001 - graph is optional: never break the answer
        log.warning("graph augmentation skipped: %s", type(exc).__name__)
        return base
