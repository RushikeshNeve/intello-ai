"""M12.09 prompt shadow runs: sampling, one comparison row per sampled run, live answer unchanged,
no text in storage, evidence deleted mid-way -> skipped, admin API and cross-tenant concealment.
Real PostgreSQL + RLS, the real worker role, an in-process run executor."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from catalog_support import World
from chat_support import HERON, HERON_ANSWER, messages, run_row
from ret_support import Rig
from sqlalchemy import Engine, text
from test_chat_executor import Rigged

from app.modules.ai.models.fake import FakeChatModel
from app.modules.ai.models.types import ChatMessage
from app.modules.evals import shadow_runner
from app.modules.prompts import shadow

MARK = "ZEBRA-CANDIDATE-MARK"
CANDIDATE_BODY = f"Answer briefly and cite every claim as [n]. {MARK}"
SHADOW_ON = {"PROMPT_SHADOW_ENABLED": True}


@pytest.fixture
def r(world: World, rig: Rig, worker_engine: Engine, base_settings: Any) -> Rigged:
    return Rigged(world, rig, worker_engine, base_settings)


@pytest.fixture(autouse=True)
def _sampler() -> Iterator[None]:
    shadow.set_sampler(lambda: 0.0)  # "100%": any rate > 0 is always sampled
    yield
    shadow.set_sampler(None)


def base(w: World) -> str:
    return f"/v1/orgs/{w.org_id}/prompt-shadow"


def candidate(w: World, rate: int = 20) -> str:
    """Create a draft and mark it as the shadow candidate; returns its version id."""
    res = w.api.post(
        f"/v1/orgs/{w.org_id}/prompts/chat_system/versions", w.t.admin, {"body": CANDIDATE_BODY}
    )
    assert res.status == 201, res.body
    vid = str(res.body["id"])
    put = w.api.put(f"{base(w)}/chat_system", w.t.admin, {"version_id": vid, "rate_pct": rate})
    assert put.status == 200, put.body
    return vid


def shadow_rows(w: World) -> list[dict[str, Any]]:
    with w.engine.begin() as conn:
        rows = (
            conn.execute(
                text("SELECT * FROM shadow_runs WHERE org_id = :o ORDER BY created_at"),
                {"o": w.org_id},
            )
            .mappings()
            .all()
        )
    return [dict(x) for x in rows]


def complete_run(r: Rigged, model: Any, **settings: Any) -> uuid.UUID:
    _, run = r.start(r.world.viewer)
    assert r.execute(run, r.deps(model), **settings) == "completed"
    shadow_runner.drain()
    return run


def test_rate_zero_or_flag_off_makes_no_shadow_run(r: Rigged) -> None:
    w = r.world
    r.publish(HERON)
    candidate(w, rate=0)
    complete_run(r, FakeChatModel([HERON_ANSWER]), **SHADOW_ON)  # rate 0
    assert shadow_rows(w) == []
    vid = candidate_id(w)
    put = w.api.put(f"{base(w)}/chat_system", w.t.admin, {"version_id": vid, "rate_pct": 20})
    assert put.status == 200
    complete_run(r, FakeChatModel([HERON_ANSWER]))  # flag off (default)
    assert shadow_rows(w) == []
    over = w.api.put(f"{base(w)}/chat_system", w.t.admin, {"version_id": vid, "rate_pct": 21})
    assert over.status == 422  # ceiling 20 %


def candidate_id(w: World) -> str:
    return str(w.api.get(base(w), w.t.admin).body["candidate"]["version_id"])


def test_sampled_run_stores_one_numbers_only_row_and_leaves_live_unchanged(r: Rigged) -> None:
    w = r.world
    r.publish(HERON)
    vid = candidate(w)
    plain = FakeChatModel([HERON_ANSWER])
    run_a = complete_run(r, plain)  # shadow flag off: the live baseline
    live_text = messages(w.engine, run_row(w.engine, run_a)["thread_id"])[1]["content"]

    prompts_seen: list[str] = []

    def reply(msgs: Sequence[ChatMessage]) -> str:
        prompts_seen.append(msgs[0]["content"])
        return HERON_ANSWER

    run_b = complete_run(r, FakeChatModel(reply_fn=reply), **SHADOW_ON)
    rows = shadow_rows(w)
    assert len(rows) == 1
    row = rows[0]
    assert (str(row["run_id"]), str(row["candidate_version_id"])) == (str(run_b), vid)
    assert (row["status"], row["live_status"], row["cand_status"]) == (
        "completed",
        "grounded",
        "grounded",
    )
    assert row["outcome"] == "tie"
    assert row["cand_latency_ms"] is not None
    assert row["tokens_in"] > 0
    assert row["tokens_out"] > 0
    assert len(prompts_seen) == 2
    assert MARK not in prompts_seen[0]  # the live call used the live prompt
    assert MARK in prompts_seen[1]  # the shadow call used the candidate
    # live answer unchanged by shadowing
    live_b = messages(w.engine, run_row(w.engine, run_b)["thread_id"])[1]["content"]
    assert live_b == live_text == HERON_ANSWER
    # no text anywhere in the row: only ids, enums and numbers
    for key, value in row.items():
        assert isinstance(value, uuid.UUID | int | float | type(None)) or key in (
            "status",
            "skip_reason",
            "live_status",
            "cand_status",
            "outcome",
            "kind",
            "created_at",
            "finished_at",
        ), key
    blob = repr(row)
    assert "Herons" not in blob
    assert HERON_ANSWER not in blob
    cols = set(row)
    assert not {"question", "answer", "text", "content", "body"} & cols
    # a run replay never yields a second row for the same (run, candidate)
    assert len(shadow_rows(w)) == 1

    # admin API: runs + aggregate
    runs = w.api.get(f"{base(w)}/runs", w.t.admin).body["items"]
    assert [x["run_id"] for x in runs] == [str(run_b)]
    res = w.api.get(f"{base(w)}/results", w.t.owner).body["items"]
    assert len(res) == 1
    assert (res[0]["total"], res[0]["completed"], res[0]["ties"], res[0]["wins"]) == (1, 1, 1, 0)
    assert res[0]["latency_delta_ms"] is not None
    # members do not see results
    assert w.api.get(f"{base(w)}/results", w.t.member).status == 403


def test_candidate_failure_is_recorded_and_live_run_is_unaffected(r: Rigged) -> None:
    w = r.world
    r.publish(HERON)
    candidate(w)

    def reply(msgs: Sequence[ChatMessage]) -> str:
        if MARK in msgs[0]["content"]:
            raise RuntimeError("candidate model blew up")
        return HERON_ANSWER

    run = complete_run(r, FakeChatModel(reply_fn=reply), **SHADOW_ON)
    row = shadow_rows(w)[0]
    assert (row["status"], row["skip_reason"], row["outcome"]) == ("failed", "error", None)
    assert messages(w.engine, run_row(w.engine, run)["thread_id"])[1]["content"] == HERON_ANSWER


def test_evidence_deleted_while_candidate_writes_is_skipped(r: Rigged) -> None:
    w = r.world
    pub = r.publish(HERON)
    candidate(w)

    def reply(msgs: Sequence[ChatMessage]) -> str:
        if MARK in msgs[0]["content"]:
            with w.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE documents SET deleted_at = now(), deleted_by = created_by"
                        " WHERE id = :d"
                    ),
                    {"d": pub.document_id},
                )
        return HERON_ANSWER

    complete_run(r, FakeChatModel(reply_fn=reply), **SHADOW_ON)
    row = shadow_rows(w)[0]
    assert (row["status"], row["skip_reason"]) == ("skipped", "evidence_deleted")
    assert row["cand_status"] is None
    assert row["tokens_in"] is None


def test_daily_cap_bounds_cost(r: Rigged) -> None:
    w = r.world
    r.publish(HERON)
    candidate(w)
    for _ in range(2):
        complete_run(r, FakeChatModel([HERON_ANSWER]), PROMPT_SHADOW_MAX_PER_DAY=1, **SHADOW_ON)
    assert len(shadow_rows(w)) == 1


def test_cross_tenant_is_concealed(r: Rigged) -> None:
    w = r.world
    r.publish(HERON)
    vid = candidate(w)
    complete_run(r, FakeChatModel([HERON_ANSWER]), **SHADOW_ON)
    f = w.foreign
    # another org's admin: cannot use this org's version (404) and sees none of its rows
    foreign_base = f"/v1/orgs/{f.org_id}/prompt-shadow"
    put = w.api.put(f"{foreign_base}/chat_system", f.admin, {"version_id": vid, "rate_pct": 5})
    assert put.status == 404
    assert w.api.get(f"{foreign_base}/runs", f.admin).body == {"items": []}
    # ... and cannot read this org's results through its own admin rights
    assert w.api.get(f"{base(w)}/results", f.admin).status == 404
    # a non-draft (active) version cannot be a candidate
    act = w.api.post(f"/v1/orgs/{w.org_id}/prompts/versions/{vid}/activate", w.t.admin, {})
    assert act.status == 200
    assert (
        w.api.put(f"{base(w)}/chat_system", w.t.admin, {"version_id": vid, "rate_pct": 5}).status
        == 409
    )


def test_org_at_a_blocking_quota_gets_no_shadow_run(r: Rigged) -> None:
    """M11.08 x M12.09: the live run uses the last allowed request; the shadow is not started."""
    w = r.world
    r.publish(HERON)
    candidate(w)
    res = w.api.post(
        f"/v1/orgs/{w.org_id}/usage/limits",
        w.t.admin,
        {"kind": "chat", "scope": "org", "action": "block", "max_requests": 1},
    )
    assert res.status == 201, res.body
    complete_run(r, FakeChatModel([HERON_ANSWER]), **SHADOW_ON)
    assert shadow_rows(w) == []


def test_style_registry_and_shadow_compose_without_interfering(r: Rigged) -> None:
    """M12.01 x M12.07 x M12.09: live = activated registry body + org style; the shadow call is
    the candidate body + the SAME style (fenced after the scaffold); no body leaks across."""
    w = r.world
    r.publish(HERON)
    live_mark = "HERON-LIVE-REGISTRY-MARK"
    res = w.api.post(
        f"/v1/orgs/{w.org_id}/prompts/chat_system/versions",
        w.t.admin,
        {"body": f"Answer and cite every claim as [n]. {live_mark}"},
    )
    assert res.status == 201, res.body
    act = w.api.post(
        f"/v1/orgs/{w.org_id}/prompts/versions/{res.body['id']}/activate", w.t.admin, {}
    )
    assert act.status == 200, act.body
    style = w.api.put(f"/v1/orgs/{w.org_id}/style", w.t.admin, {"tone": "formal"})
    assert style.status == 200, style.body
    candidate(w)
    seen: list[str] = []

    def reply(msgs: Sequence[ChatMessage]) -> str:
        seen.append(msgs[0]["content"])
        return HERON_ANSWER

    complete_run(r, FakeChatModel(reply_fn=reply), **SHADOW_ON)
    assert len(seen) == 2
    live, cand = seen
    assert live_mark in live
    assert MARK not in live
    assert MARK in cand
    assert live_mark not in cand
    for system in (live, cand):
        assert "<<<STYLE PREFERENCES>>>" in system
        assert "formal tone" in system
        assert system.index("<<<STYLE PREFERENCES>>>") > system.index(
            live_mark if system is live else MARK
        )
    assert len(shadow_rows(w)) == 1
