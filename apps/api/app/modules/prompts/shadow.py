"""Prompt shadow runs, storage side (M12.09). Pure SQL + small pure helpers; no model, no chat.

A shadow candidate is a draft prompt version of an org for a kind, with a sampling rate of 0-20 %.
Sampled real chat runs are re-generated with the candidate on the SAME evidence (see
``app.modules.evals.shadow_runner``) and compared with the live run. ``shadow_runs`` keeps ids,
statuses and numbers ONLY: never a question, an answer or an evidence text.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

KIND: Final = "chat_system"
MAX_RATE_PCT: Final = 20
STATUS_RANK: Final = {"grounded": 2, "partial": 1, "abstained": 0}

_sampler: Callable[[], float] = lambda: random.random() * 100  # noqa: E731, S311 - not crypto


def set_sampler(sampler: Callable[[], float] | None) -> None:
    """Tests only: a sampler returning a value in [0, 100); ``None`` restores the random one."""
    global _sampler
    _sampler = sampler or (lambda: random.random() * 100)  # noqa: S311


def sampled(rate_pct: int) -> bool:
    return rate_pct > 0 and _sampler() < rate_pct


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    """What the live run produced, as numbers (kept in memory only until the row is written)."""

    status: str
    citation_validity: float | None
    latency_ms: int
    prompt_version_id: uuid.UUID | None


def citation_validity(claims: Any) -> float | None:
    """Share of cited claims that are supported (port of ``rag_metrics.citation_validity`` over the
    verifier's claim units). ``None`` when no claim carries a citation marker."""
    cited = [c for c in claims if c.markers]
    if not cited:
        return None
    return sum(1 for c in cited if c.status == "supported") / len(cited)


def outcome(
    live_status: str,
    live_validity: float | None,
    cand_status: str,
    cand_validity: float | None,
) -> str:
    """win/tie/loss of the candidate: verification status first, then citation validity."""
    a, b = STATUS_RANK.get(cand_status, 0), STATUS_RANK.get(live_status, 0)
    if a != b:
        return "win" if a > b else "loss"
    if cand_validity is None or live_validity is None or abs(cand_validity - live_validity) < 1e-9:
        return "tie"
    return "win" if cand_validity > live_validity else "loss"


# ---------------------------------------------------------------------------------- candidates


def get_candidate(session: Session, org_id: uuid.UUID, kind: str = KIND) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT c.org_id, c.kind, c.version_id, c.rate_pct, c.updated_at,"
                " v.version, v.state"
                " FROM shadow_candidates c JOIN prompt_versions v ON v.id = c.version_id"
                " WHERE c.org_id = :o AND c.kind = :k"
            ),
            {"o": org_id, "k": kind},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


def set_candidate(
    session: Session,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    version_id: uuid.UUID,
    rate_pct: int,
    kind: str = KIND,
) -> None:
    session.execute(
        text(
            "INSERT INTO shadow_candidates (org_id, kind, version_id, rate_pct, updated_by)"
            " VALUES (:o, :k, :v, :r, :u)"
            " ON CONFLICT (org_id, kind) DO UPDATE SET version_id = :v, rate_pct = :r,"
            " updated_by = :u, updated_at = now()"
        ),
        {"o": org_id, "k": kind, "v": version_id, "r": rate_pct, "u": user_id},
    )


def clear_candidate(session: Session, org_id: uuid.UUID, kind: str = KIND) -> bool:
    res = session.execute(
        text("DELETE FROM shadow_candidates WHERE org_id = :o AND kind = :k"),
        {"o": org_id, "k": kind},
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]


def candidate_body(session: Session, org_id: uuid.UUID, version_id: uuid.UUID) -> str | None:
    row = session.execute(
        text("SELECT body FROM prompt_versions WHERE id = :i AND org_id = :o"),
        {"i": version_id, "o": org_id},
    ).scalar_one_or_none()
    return None if row is None else str(row)


# ------------------------------------------------------------------------------------ run rows


def runs_today(session: Session, org_id: uuid.UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM shadow_runs WHERE org_id = :o"
                " AND created_at >= date_trunc('day', now())"
            ),
            {"o": org_id},
        ).scalar_one()
    )


def insert_running(
    session: Session,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    version_id: uuid.UUID,
    live: LiveSnapshot,
) -> uuid.UUID | None:
    """The comparison row in ``running`` state; ``None`` when (run, candidate) already has one."""
    new_id = uuid.uuid4()
    row = session.execute(
        text(
            "INSERT INTO shadow_runs (id, org_id, run_id, candidate_version_id, live_status,"
            " live_citation_validity, live_latency_ms)"
            " VALUES (:i, :o, :r, :v, :s, :cv, :l)"
            " ON CONFLICT (run_id, candidate_version_id) DO NOTHING RETURNING id"
        ),
        {
            "i": new_id,
            "o": org_id,
            "r": run_id,
            "v": version_id,
            "s": live.status,
            "cv": live.citation_validity,
            "l": live.latency_ms,
        },
    ).scalar_one_or_none()
    return None if row is None else new_id


def finish_row(session: Session, shadow_id: uuid.UUID, **values: Any) -> None:
    """Set the final columns (``status`` and any of the numeric / id columns) of one row."""
    allowed = {
        "status",
        "skip_reason",
        "cand_status",
        "cand_citation_validity",
        "cand_latency_ms",
        "tokens_in",
        "tokens_out",
        "outcome",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown shadow column(s): {sorted(unknown)}")
    sets = ", ".join(f"{k} = :{k}" for k in values)
    session.execute(
        text(f"UPDATE shadow_runs SET {sets}, finished_at = now() WHERE id = :id"),  # noqa: S608 - allow-listed columns
        {**values, "id": shadow_id},
    )


RUN_COLUMNS: Final = (
    "id, run_id, candidate_version_id, status, skip_reason, live_status, live_citation_validity,"
    " live_latency_ms, cand_status, cand_citation_validity, cand_latency_ms, tokens_in,"
    " tokens_out, outcome, created_at, finished_at"
)


def list_runs(
    session: Session, org_id: uuid.UUID, candidate_id: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            f"SELECT {RUN_COLUMNS} FROM shadow_runs WHERE org_id = :o"  # noqa: S608 - constant
            " AND (CAST(:c AS uuid) IS NULL OR candidate_version_id = :c)"
            " ORDER BY created_at DESC LIMIT :n"
        ),
        {"o": org_id, "c": candidate_id, "n": limit},
    ).mappings()
    return [dict(r) for r in rows]


def aggregate(session: Session, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """Per candidate: counts by status, win/tie/loss and the mean latency / token numbers of the
    completed comparisons (``latency_delta_ms`` = candidate minus live; negative = faster)."""
    rows = session.execute(
        text(
            "SELECT r.candidate_version_id, v.version, v.state,"
            " count(*) AS total,"
            " count(*) FILTER (WHERE r.status = 'completed') AS completed,"
            " count(*) FILTER (WHERE r.status = 'skipped') AS skipped,"
            " count(*) FILTER (WHERE r.status = 'failed') AS failed,"
            " count(*) FILTER (WHERE r.outcome = 'win') AS wins,"
            " count(*) FILTER (WHERE r.outcome = 'tie') AS ties,"
            " count(*) FILTER (WHERE r.outcome = 'loss') AS losses,"
            " avg(r.cand_latency_ms - r.live_latency_ms) FILTER (WHERE r.status = 'completed')"
            " AS latency_delta_ms,"
            " avg(r.tokens_in) FILTER (WHERE r.status = 'completed') AS mean_tokens_in,"
            " avg(r.tokens_out) FILTER (WHERE r.status = 'completed') AS mean_tokens_out"
            " FROM shadow_runs r JOIN prompt_versions v ON v.id = r.candidate_version_id"
            " WHERE r.org_id = :o GROUP BY r.candidate_version_id, v.version, v.state"
            " ORDER BY v.version DESC"
        ),
        {"o": org_id},
    ).mappings()
    return [
        {k: (float(v) if k in _FLOATS and v is not None else v) for k, v in dict(r).items()}
        for r in rows
    ]


_FLOATS: Final = {"latency_delta_ms", "mean_tokens_in", "mean_tokens_out"}
