"""Per-workspace entity resolution and merge review (M05.01.03, contract m05 section 3).

* The canonical id is deterministic (``ids.entity_id_for``), so an exact normalized-name match
  of the same type IS the same row: exact merge, with the surface form added as an alias.
* Fuzzy matches (token Jaccard >= 0.8, or edit distance <= 2 for names of 5+ characters, same type
  only) never merge: they become ``entity_merge_candidates`` a human accepts or rejects.
* The same name with another type is a different entity; :func:`list_conflicts` lists those groups.
* An accepted merge marks the loser ``merged`` with ``merged_into`` (the alias mapping); later
  extractions that hit the loser's id follow the chain to the canonical entity.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.graph.extract import ExtractedEntity
from app.modules.graph.ids import entity_id_for
from app.modules.graph.models import EntityMergeCandidate, GraphEntity
from app.modules.graph.ontology import ENTITY_ACTIVE, ENTITY_MERGED

JACCARD_MIN: Final = 0.8
EDIT_DISTANCE_MAX: Final = 2
EDIT_MIN_LENGTH: Final = 5
MAX_ALIASES: Final = 20
MAX_POOL: Final = 5000
_CHAIN_LIMIT: Final = 10


class CandidateNotFound(Exception):
    pass


class AlreadyDecided(Exception):
    pass


class NotMergeable(Exception):
    """One of the two entities is no longer active (already merged elsewhere)."""


def _edit_distance(a: str, b: str, cap: int) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float | None:
    """Candidate score of two normalized names of one type, or ``None`` when they are unrelated."""
    if a == b:
        return None  # identical normalized names are the same entity, not a candidate
    ta, tb = set(a.split()), set(b.split())
    best = 0.0
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        if jaccard >= JACCARD_MIN:
            best = jaccard
    if min(len(a), len(b)) >= EDIT_MIN_LENGTH:
        dist = _edit_distance(a, b, EDIT_DISTANCE_MAX)
        if dist <= EDIT_DISTANCE_MAX:
            best = max(best, 1.0 - dist / max(len(a), len(b)))
    return best if best > 0 else None


@dataclass(slots=True)
class CanonicalEntity:
    id: uuid.UUID
    type: str
    canonical_name: str
    aliases: list[str]


@dataclass(slots=True)
class Resolved:
    ids: dict[tuple[str, str], uuid.UUID] = field(default_factory=dict)
    entities: list[CanonicalEntity] = field(default_factory=list)
    candidates: int = 0


def _canonical(session: Session, entity_id: uuid.UUID) -> GraphEntity | None:
    """Follow ``merged_into`` to the live entity (``None`` for a rejected/unknown one)."""
    row = session.get(GraphEntity, entity_id)
    for _ in range(_CHAIN_LIMIT):
        if row is None or row.state != ENTITY_MERGED or row.merged_into is None:
            break
        row = session.get(GraphEntity, row.merged_into)
    return row if row is not None and row.state == ENTITY_ACTIVE else None


def resolve_entities(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    extracted: Iterable[ExtractedEntity],
) -> Resolved:
    """Create/merge the identities of one extraction. Runs in the worker transaction."""
    result = Resolved()
    pools: dict[str, list[tuple[uuid.UUID, str]]] = {}
    seen: set[uuid.UUID] = set()
    for ent in extracted:
        eid = entity_id_for(org_id, workspace_id, ent.type, ent.name)
        inserted = session.execute(
            pg_insert(GraphEntity)
            .values(
                id=eid,
                org_id=org_id,
                workspace_id=workspace_id,
                type=ent.type,
                canonical_name=ent.name,
                normalized_name=ent.normalized,
                aliases=[],
            )
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(GraphEntity.id)
        ).first()
        if ent.type not in pools:
            rows = session.execute(
                select(GraphEntity.id, GraphEntity.normalized_name)
                .where(
                    GraphEntity.workspace_id == workspace_id,
                    GraphEntity.type == ent.type,
                    GraphEntity.state == ENTITY_ACTIVE,
                )
                .limit(MAX_POOL)
            ).all()
            pools[ent.type] = [(r.id, r.normalized_name) for r in rows]
        pool = pools[ent.type]
        if inserted is not None:
            if all(pid != eid for pid, _ in pool):
                pool.append((eid, ent.normalized))
            for other_id, other_norm in pool:
                if other_id == eid:
                    continue
                score = similarity(ent.normalized, other_norm)
                if score is None:
                    continue
                a, b = sorted((eid, other_id))
                session.execute(
                    pg_insert(EntityMergeCandidate)
                    .values(
                        org_id=org_id,
                        workspace_id=workspace_id,
                        entity_a=a,
                        entity_b=b,
                        score=score,
                    )
                    .on_conflict_do_nothing(constraint="uq_entity_merge_candidates_pair")
                )
                result.candidates += 1
        canonical = _canonical(session, eid)
        if canonical is None:
            continue  # a rejected identity: nothing may attach to it
        aliases = [str(a) for a in canonical.aliases]
        new_aliases = [
            s
            for s in sorted(ent.surfaces | {ent.name})
            if s != canonical.canonical_name and s not in aliases
        ]
        if new_aliases and len(aliases) < MAX_ALIASES:
            aliases = (aliases + new_aliases)[:MAX_ALIASES]
            session.execute(
                update(GraphEntity).where(GraphEntity.id == canonical.id).values(aliases=aliases)
            )
        result.ids[ent.key] = canonical.id
        if canonical.id not in seen:
            seen.add(canonical.id)
            result.entities.append(
                CanonicalEntity(canonical.id, canonical.type, canonical.canonical_name, aliases)
            )
    return result


# ------------------------------------------------------------------------------- review


@dataclass(frozen=True, slots=True)
class CandidateView:
    id: uuid.UUID
    score: float
    state: str
    entity_a: GraphEntity
    entity_b: GraphEntity


def list_candidates(
    session: Session, workspace_id: uuid.UUID, *, state: str = "pending", limit: int = 50
) -> list[CandidateView]:
    rows = session.execute(
        select(EntityMergeCandidate)
        .where(
            EntityMergeCandidate.workspace_id == workspace_id, EntityMergeCandidate.state == state
        )
        .order_by(EntityMergeCandidate.score.desc(), EntityMergeCandidate.id)
        .limit(limit)
    ).scalars()
    out: list[CandidateView] = []
    for cand in rows:
        a = session.get(GraphEntity, cand.entity_a)
        b = session.get(GraphEntity, cand.entity_b)
        if a is not None and b is not None:
            out.append(CandidateView(cand.id, cand.score, cand.state, a, b))
    return out


def list_conflicts(
    session: Session, workspace_id: uuid.UUID, *, limit: int = 50
) -> list[tuple[str, list[GraphEntity]]]:
    """Active entities sharing a normalized name under different types (kept separate)."""
    names = (
        session.execute(
            select(GraphEntity.normalized_name)
            .where(GraphEntity.workspace_id == workspace_id, GraphEntity.state == ENTITY_ACTIVE)
            .group_by(GraphEntity.normalized_name)
            .having(func.count(func.distinct(GraphEntity.type)) > 1)
            .order_by(GraphEntity.normalized_name)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    out: list[tuple[str, list[GraphEntity]]] = []
    for name in names:
        members = list(
            session.execute(
                select(GraphEntity)
                .where(
                    GraphEntity.workspace_id == workspace_id,
                    GraphEntity.normalized_name == name,
                    GraphEntity.state == ENTITY_ACTIVE,
                )
                .order_by(GraphEntity.type)
            ).scalars()
        )
        out.append((name, members))
    return out


@dataclass(frozen=True, slots=True)
class Decision:
    state: str
    winner_id: uuid.UUID | None = None
    loser_id: uuid.UUID | None = None


def decide_candidate(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    candidate_id: uuid.UUID,
    accept: bool,
    user_id: uuid.UUID,
) -> Decision:
    """Accept (merge) or reject one pending candidate of the workspace."""
    cand = session.execute(
        select(EntityMergeCandidate)
        .where(
            EntityMergeCandidate.id == candidate_id,
            EntityMergeCandidate.workspace_id == workspace_id,
            EntityMergeCandidate.org_id == org_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if cand is None:
        raise CandidateNotFound
    if cand.state != "pending":
        raise AlreadyDecided
    winner_id: uuid.UUID | None = None
    loser_id: uuid.UUID | None = None
    if accept:
        rows = {
            e.id: e
            for e in session.execute(
                select(GraphEntity)
                .where(GraphEntity.id.in_([cand.entity_a, cand.entity_b]))
                .with_for_update()
            ).scalars()
        }
        a, b = rows.get(cand.entity_a), rows.get(cand.entity_b)
        if a is None or b is None or a.state != ENTITY_ACTIVE or b.state != ENTITY_ACTIVE:
            raise NotMergeable
        winner, loser = sorted((a, b), key=lambda e: (e.created_at, str(e.id)))
        aliases = [str(x) for x in winner.aliases]
        for name in [loser.canonical_name, *[str(x) for x in loser.aliases]]:
            if name != winner.canonical_name and name not in aliases:
                aliases.append(name)
        session.execute(
            update(GraphEntity)
            .where(GraphEntity.id == winner.id)
            .values(aliases=aliases[:MAX_ALIASES])
        )
        session.execute(
            update(GraphEntity)
            .where(GraphEntity.id == loser.id)
            .values(state=ENTITY_MERGED, merged_into=winner.id)
        )
        winner_id, loser_id = winner.id, loser.id
    new_state = "accepted" if accept else "rejected"
    session.execute(
        update(EntityMergeCandidate)
        .where(EntityMergeCandidate.id == candidate_id)
        .values(state=new_state, decided_by=user_id, decided_at=func.now())
    )
    return Decision(new_state, winner_id, loser_id)
