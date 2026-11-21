# ruff: noqa: S608  (f-strings splice only module constants / fixed column lists, never input)
"""Golden-set data access and the (pure) metric computation. Every function takes a tenant session
of the path org; RLS limits every table to workspace admins of the row's workspace."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.evals.schemas import MAX_CASES_PER_SET, CaseIn, CasePatch

SET_COLS: Final = "id, workspace_id, name, version, created_by, created_at, updated_at"
CASE_COLS: Final = (
    "id, set_id, question, expected_document_ids, expected_chunk_ids, should_abstain, notes,"
    " created_at"
)
RUN_COLS: Final = (
    "id, set_id, set_version, state, config, summary, created_by, started_at, finished_at"
)


class SetFull(Exception):
    """The set already holds ``MAX_CASES_PER_SET`` cases."""


def _ids(values: Sequence[uuid.UUID] | None) -> str:
    return json.dumps([str(v) for v in values or []])


# ------------------------------------------------------------------------------------- sets


def list_sets(session: Session, ws: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            f"SELECT {SET_COLS}, (SELECT count(*) FROM eval_cases c WHERE c.set_id = s.id)"
            " AS case_count FROM eval_sets s WHERE workspace_id = :w ORDER BY created_at, id"
        ),
        {"w": ws},
    )
    return [dict(r._mapping) for r in rows]


def get_set(session: Session, ws: uuid.UUID, set_id: uuid.UUID) -> dict[str, Any] | None:
    row = session.execute(
        text(
            f"SELECT {SET_COLS}, (SELECT count(*) FROM eval_cases c WHERE c.set_id = s.id)"
            " AS case_count FROM eval_sets s WHERE id = :i AND workspace_id = :w"
        ),
        {"i": set_id, "w": ws},
    ).one_or_none()
    return dict(row._mapping) if row is not None else None


def create_set(
    session: Session, org: uuid.UUID, ws: uuid.UUID, user: uuid.UUID, name: str
) -> uuid.UUID:
    set_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO eval_sets (id, org_id, workspace_id, name, created_by)"
            " VALUES (:i, :o, :w, :n, :u)"
        ),
        {"i": set_id, "o": org, "w": ws, "n": name, "u": user},
    )
    return set_id


def rename_set(session: Session, ws: uuid.UUID, set_id: uuid.UUID, name: str) -> None:
    session.execute(
        text(
            "UPDATE eval_sets SET name = :n, version = version + 1, updated_at = now()"
            " WHERE id = :i AND workspace_id = :w"
        ),
        {"n": name, "i": set_id, "w": ws},
    )


def delete_set(session: Session, ws: uuid.UUID, set_id: uuid.UUID) -> None:
    session.execute(
        text("DELETE FROM eval_sets WHERE id = :i AND workspace_id = :w"), {"i": set_id, "w": ws}
    )


def _bump(session: Session, set_id: uuid.UUID) -> None:
    session.execute(
        text("UPDATE eval_sets SET version = version + 1, updated_at = now() WHERE id = :i"),
        {"i": set_id},
    )


# ------------------------------------------------------------------------------------ cases


def list_cases(session: Session, ws: uuid.UUID, set_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            f"SELECT {CASE_COLS} FROM eval_cases WHERE set_id = :s AND workspace_id = :w"
            " ORDER BY created_at, id"
        ),
        {"s": set_id, "w": ws},
    )
    return [dict(r._mapping) for r in rows]


def get_case(
    session: Session, ws: uuid.UUID, set_id: uuid.UUID, case_id: uuid.UUID
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            f"SELECT {CASE_COLS} FROM eval_cases"
            " WHERE id = :i AND set_id = :s AND workspace_id = :w"
        ),
        {"i": case_id, "s": set_id, "w": ws},
    ).one_or_none()
    return dict(row._mapping) if row is not None else None


def add_case(
    session: Session, org: uuid.UUID, ws: uuid.UUID, set_id: uuid.UUID, body: CaseIn
) -> uuid.UUID:
    count = session.execute(
        text("SELECT count(*) FROM eval_cases WHERE set_id = :s"), {"s": set_id}
    ).scalar_one()
    if count >= MAX_CASES_PER_SET:
        raise SetFull
    case_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO eval_cases (id, org_id, workspace_id, set_id, question,"
            " expected_document_ids, expected_chunk_ids, should_abstain, notes)"
            " VALUES (:i, :o, :w, :s, :q, CAST(:d AS jsonb), CAST(:c AS jsonb), :a, :n)"
        ),
        {
            "i": case_id,
            "o": org,
            "w": ws,
            "s": set_id,
            "q": body.question,
            "d": _ids(body.expected_document_ids),
            "c": _ids(body.expected_chunk_ids),
            "a": body.should_abstain,
            "n": body.notes,
        },
    )
    _bump(session, set_id)
    return case_id


def patch_case(
    session: Session, ws: uuid.UUID, set_id: uuid.UUID, case_id: uuid.UUID, body: CasePatch
) -> None:
    sets: list[str] = []
    params: dict[str, Any] = {"i": case_id, "s": set_id, "w": ws}
    fields = body.model_fields_set
    if "question" in fields and body.question is not None:
        sets.append("question = :q")
        params["q"] = body.question
    if "expected_document_ids" in fields and body.expected_document_ids is not None:
        sets.append("expected_document_ids = CAST(:d AS jsonb)")
        params["d"] = _ids(body.expected_document_ids)
    if "expected_chunk_ids" in fields and body.expected_chunk_ids is not None:
        sets.append("expected_chunk_ids = CAST(:c AS jsonb)")
        params["c"] = _ids(body.expected_chunk_ids)
    if "should_abstain" in fields and body.should_abstain is not None:
        sets.append("should_abstain = :a")
        params["a"] = body.should_abstain
    if "notes" in fields:
        sets.append("notes = :n")
        params["n"] = body.notes
    if sets:
        session.execute(
            text(
                f"UPDATE eval_cases SET {', '.join(sets)}"
                " WHERE id = :i AND set_id = :s AND workspace_id = :w"
            ),
            params,
        )
        _bump(session, set_id)


def delete_case(session: Session, ws: uuid.UUID, set_id: uuid.UUID, case_id: uuid.UUID) -> None:
    session.execute(
        text("DELETE FROM eval_cases WHERE id = :i AND set_id = :s AND workspace_id = :w"),
        {"i": case_id, "s": set_id, "w": ws},
    )
    _bump(session, set_id)


# ------------------------------------------------------------------------------------ runs


def case_metrics(
    case: dict[str, Any], retrieved: Sequence[dict[str, Any]], *, answered: bool
) -> dict[str, Any]:
    """Per-case metrics. ``hit``/``rank`` only exist for cases that name expected sources."""
    expected_docs = {str(x) for x in case["expected_document_ids"]}
    expected_chunks = {str(x) for x in case["expected_chunk_ids"]}
    has_expectations = bool(expected_docs or expected_chunks)
    rank: int | None = None
    for position, item in enumerate(retrieved, 1):
        if str(item["document_id"]) in expected_docs or str(item["chunk_id"]) in expected_chunks:
            rank = position
            break
    should_abstain = bool(case["should_abstain"])
    hit = (rank is not None) if has_expectations else None
    abstain_correct = should_abstain == (not answered)
    passed = (not answered) if should_abstain else (hit if hit is not None else answered)
    return {
        "hit": hit,
        "rank": rank,
        "should_abstain": should_abstain,
        "abstain_correct": abstain_correct,
        "passed": bool(passed),
    }


def summarize(results: Sequence[dict[str, Any]], *, top_k: int) -> dict[str, Any]:
    """Aggregate: hit@k and MRR over cases with expected sources, abstention correctness."""
    ok = [r for r in results if r["answer_status"] != "error"]
    with_expect = [
        r for r in ok if r["metrics"]["hit"] is not None and not r["metrics"]["should_abstain"]
    ]
    hits = sum(1 for r in with_expect if r["metrics"]["hit"])
    mrr = sum(1.0 / r["metrics"]["rank"] for r in with_expect if r["metrics"]["rank"])
    abstain_cases = [r for r in ok if r["metrics"]["should_abstain"]]
    return {
        "total": len(results),
        "errors": len(results) - len(ok),
        "top_k": top_k,
        "cases_with_expected_sources": len(with_expect),
        "hit_at_k": round(hits / len(with_expect), 4) if with_expect else None,
        "mrr": round(mrr / len(with_expect), 4) if with_expect else None,
        "abstain_cases": len(abstain_cases),
        "abstention_accuracy": (
            round(
                sum(1 for r in abstain_cases if r["metrics"]["abstain_correct"])
                / len(abstain_cases),
                4,
            )
            if abstain_cases
            else None
        ),
        "abstain_correct_rate": (
            round(sum(1 for r in ok if r["metrics"]["abstain_correct"]) / len(ok), 4)
            if ok
            else None
        ),
        "pass_rate": (
            round(sum(1 for r in ok if r["metrics"]["passed"]) / len(results), 4)
            if results
            else None
        ),
    }


def insert_run(
    session: Session,
    *,
    org: uuid.UUID,
    ws: uuid.UUID,
    user: uuid.UUID,
    set_id: uuid.UUID,
    set_version: int,
    config: dict[str, Any],
    summary: dict[str, Any],
    results: Sequence[dict[str, Any]],
) -> uuid.UUID:
    run_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO eval_runs (id, org_id, workspace_id, set_id, set_version, state, config,"
            " summary, created_by, finished_at) VALUES (:i, :o, :w, :s, :v, 'completed',"
            " CAST(:c AS jsonb), CAST(:m AS jsonb), :u, now())"
        ),
        {
            "i": run_id,
            "o": org,
            "w": ws,
            "s": set_id,
            "v": set_version,
            "c": json.dumps(config),
            "m": json.dumps(summary),
            "u": user,
        },
    )
    for r in results:
        session.execute(
            text(
                "INSERT INTO eval_run_results (id, org_id, workspace_id, run_id, case_id, question,"
                " retrieved, answer_status, metrics) VALUES (:i, :o, :w, :r, :c, :q,"
                " CAST(:t AS jsonb), :a, CAST(:m AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "o": org,
                "w": ws,
                "r": run_id,
                "c": r["case_id"],
                "q": r["question"],
                "t": json.dumps(r["retrieved"]),
                "a": r["answer_status"],
                "m": json.dumps(r["metrics"]),
            },
        )
    return run_id


def list_runs(session: Session, ws: uuid.UUID, set_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            f"SELECT {RUN_COLS} FROM eval_runs WHERE set_id = :s AND workspace_id = :w"
            " ORDER BY started_at DESC, id DESC LIMIT 50"
        ),
        {"s": set_id, "w": ws},
    )
    return [dict(r._mapping) for r in rows]


def get_run(
    session: Session, ws: uuid.UUID, set_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            f"SELECT {RUN_COLS} FROM eval_runs WHERE id = :i AND set_id = :s AND workspace_id = :w"
        ),
        {"i": run_id, "s": set_id, "w": ws},
    ).one_or_none()
    if row is None:
        return None
    run = dict(row._mapping)
    res = session.execute(
        text(
            "SELECT id, case_id, question, retrieved, answer_status, metrics"
            " FROM eval_run_results WHERE run_id = :r ORDER BY question, id"
        ),
        {"r": run_id},
    )
    run["results"] = [dict(r._mapping) for r in res]
    return run
