"""Permission-safe bounded graph traversal (M05.02.01, contract m05 §4).

Every record the repository returns is re-checked here: organization and workspace in scope,
``published``, ``verification == 'verified'``, active entity. An edge is followed only when both
endpoint entities live in the claim's own workspace (graph data never crosses workspaces). Hard
caps on depth/nodes/edges; breadth-first with a visited set, so cycles terminate. Degrees and the
``truncated`` flag are computed over the returned (authorized) subgraph only: nothing reveals how
much was hidden.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.modules.graph.ports_read import GClaim, GEntity, GraphReadRepository
from app.modules.graph.read_settings import HARD_MAX_DEPTH, HARD_MAX_EDGES, HARD_MAX_NODES
from app.modules.retrieval.scope import RetrievalScope


@dataclass(slots=True)
class Subgraph:
    nodes: dict[uuid.UUID, GEntity] = field(default_factory=dict)
    edges: list[GClaim] = field(default_factory=list)
    hop: dict[uuid.UUID, int] = field(default_factory=dict)  # claim id -> hop (1-based)
    truncated: bool = False

    def degree(self, entity_id: uuid.UUID) -> int:
        return sum(1 for c in self.edges if entity_id in (c.subject_id, c.object_id))


def entity_visible(scope: RetrievalScope, e: GEntity) -> bool:
    return (
        e.org_id == scope.org_id
        and e.workspace_id in scope.workspace_ids
        and e.entity_state == "active"
    )


def claim_visible(scope: RetrievalScope, c: GClaim) -> bool:
    return (
        c.org_id == scope.org_id
        and c.workspace_id in scope.workspace_ids
        and c.published is True
        and c.verification == "verified"
    )


def traverse(
    scope: RetrievalScope,
    repo: GraphReadRepository,
    seed_entity_ids: Sequence[uuid.UUID],
    *,
    depth: int = 2,
    max_nodes: int = 50,
    max_edges: int = 100,
) -> Subgraph:
    depth = max(0, min(depth, HARD_MAX_DEPTH))
    max_nodes = max(1, min(max_nodes, HARD_MAX_NODES))
    max_edges = max(1, min(max_edges, HARD_MAX_EDGES))
    out = Subgraph()
    seeds = [
        e
        for e in repo.get_entities(scope, list(dict.fromkeys(seed_entity_ids)))
        if entity_visible(scope, e)
    ]
    for e in seeds[:max_nodes]:
        out.nodes[e.id] = e
    out.truncated = len(seeds) > max_nodes
    frontier = list(out.nodes)
    seen_claims: set[uuid.UUID] = set()
    for hop in range(1, depth + 1):
        if not frontier:
            break
        frontier_set = set(frontier)
        claims = repo.neighbors(scope, frontier, max_edges * 2)
        usable: list[GClaim] = []
        for c in claims:
            if (
                c.id in seen_claims
                or c.object_id is None
                or not claim_visible(scope, c)
                or not ({c.subject_id, c.object_id} & frontier_set)
            ):
                continue
            usable.append(c)
        usable.sort(key=lambda c: (-c.confidence, str(c.id)))
        unknown = list(
            {i for c in usable for i in (c.subject_id, c.object_id) if i is not None}
            - set(out.nodes)
        )
        fetched = {e.id: e for e in repo.get_entities(scope, unknown) if entity_visible(scope, e)}
        known = {**out.nodes, **fetched}
        added: list[uuid.UUID] = []
        for c in usable:
            if c.object_id is None:
                continue
            ends = [known.get(c.subject_id), known.get(c.object_id)]
            if any(e is None or e.workspace_id != c.workspace_id for e in ends):
                continue  # unknown/hidden endpoint or an edge that would cross workspaces
            if len(out.edges) >= max_edges:
                out.truncated = True
                break
            new = {e.id for e in ends if e is not None and e.id not in out.nodes}
            if len(out.nodes) + len(new) > max_nodes:
                out.truncated = True
                continue
            for i in new:
                out.nodes[i] = known[i]
                added.append(i)
            out.edges.append(c)
            out.hop[c.id] = hop
            seen_claims.add(c.id)
        frontier = added
    return out
