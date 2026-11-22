"""Prompt shadow execution (M12.09), run by the chat worker AFTER the live run is completed.

The live answer is already persisted and streamed; nothing here can change it: every failure is
swallowed and recorded as a ``failed`` / ``skipped`` comparison row. The candidate prompt re-runs
generation on the SAME evidence the live run used (no new retrieval); the creator's authorization
and the evidence are re-validated before AND after the model call (a deleted document or a lost
workspace -> ``skipped``). The row holds ids, statuses and numbers only.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import AbstractContextManager
from typing import Final

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules import prompts
from app.modules.ai import chat_repo
from app.modules.ai.chat import ChatDeps, ShadowSeed, call_with_timeout
from app.modules.ai.context import build_prompt
from app.modules.ai.models.types import estimate_tokens
from app.modules.ai.policy_source import DbPolicySource
from app.modules.ai.verification import VerifyConfig, verify
from app.modules.prompts import shadow, styles
from app.modules.usage.limits import QuotaExceeded, QuotaGuard

log = logging.getLogger(__name__)

SKIP_EVIDENCE_DELETED: Final = "evidence_deleted"
SKIP_CANDIDATE_MISSING: Final = "candidate_missing"
SKIP_NO_BUDGET: Final = "insufficient_budget"
FAIL_ERROR: Final = "error"

_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()


def drain(timeout: float = 30.0) -> None:
    """Wait for in-flight shadow threads (tests and graceful shutdown)."""
    with _threads_lock:
        pending = list(_threads)
    for t in pending:
        t.join(timeout)
    with _threads_lock:
        _threads[:] = [t for t in _threads if t.is_alive()]


def maybe_shadow(
    *,
    engine: Engine,
    settings: Settings,
    deps: ChatDeps,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
    seed: ShadowSeed,
    background: bool = True,
) -> uuid.UUID | None:
    """Sample this completed run and, when chosen, start its shadow run. Never raises. Returns the
    comparison row id (``None`` = not sampled / off / over the daily cap)."""
    if not settings.PROMPT_SHADOW_ENABLED:
        return None
    if _over_quota(engine, org_id, user_id):
        return None  # M11.08: an org (or user) at a blocking limit gets no extra model calls
    try:
        with chat_repo.worker_chat_session(engine, org_id=org_id, user_id=user_id) as session:
            cand = shadow.get_candidate(session, org_id)
            if cand is None or not shadow.sampled(int(cand["rate_pct"])):
                return None
            version_id = cand["version_id"]
            if version_id == seed.prompt_version_id:
                return None  # the candidate IS the live prompt: nothing to compare
            if shadow.runs_today(session, org_id) >= settings.PROMPT_SHADOW_MAX_PER_DAY:
                return None
            live = shadow.LiveSnapshot(
                seed.verification.status,
                shadow.citation_validity(seed.verification.claims),
                seed.latency_ms,
                seed.prompt_version_id,
            )
            shadow_id = shadow.insert_running(session, org_id, run_id, version_id, live)
    except Exception as exc:  # noqa: BLE001 - shadowing must never disturb the live run
        log.warning("shadow run=%s: not scheduled (%s)", run_id, type(exc).__name__)
        return None
    if shadow_id is None:
        return None

    def work() -> None:
        _execute(engine, deps, org_id, user_id, shadow_id, version_id, seed, live)

    if not background:
        work()
        return shadow_id
    thread = threading.Thread(target=work, name=f"shadow-{shadow_id}", daemon=True)
    with _threads_lock:
        _threads.append(thread)
    thread.start()
    return shadow_id


def _over_quota(engine: Engine, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """True when a blocking chat limit is reached. The guard fails open on storage errors."""

    def sessions(org: uuid.UUID) -> AbstractContextManager[Session]:
        return chat_repo.worker_chat_session(engine, org_id=org, user_id=user_id)

    try:
        QuotaGuard(sessions, org_id=org_id, user_id=None).check("chat")
    except QuotaExceeded:
        return True
    return False


def _finish(
    engine: Engine, org_id: uuid.UUID, user_id: uuid.UUID, shadow_id: uuid.UUID, **values: object
) -> None:
    try:
        with chat_repo.worker_chat_session(engine, org_id=org_id, user_id=user_id) as session:
            shadow.finish_row(session, shadow_id, **values)
    except Exception as exc:  # noqa: BLE001
        log.warning("shadow row=%s: could not be finished (%s)", shadow_id, type(exc).__name__)


def _execute(
    engine: Engine,
    deps: ChatDeps,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    shadow_id: uuid.UUID,
    version_id: uuid.UUID,
    seed: ShadowSeed,
    live: shadow.LiveSnapshot,
) -> None:
    def done(**values: object) -> None:
        _finish(engine, org_id, user_id, shadow_id, **values)

    try:
        if not seed.still_valid():
            return done(status="skipped", skip_reason=SKIP_EVIDENCE_DELETED)
        cs = deps.chat_settings
        with chat_repo.worker_chat_session(engine, org_id=org_id, user_id=user_id) as session:
            body = shadow.candidate_body(session, org_id, version_id)
            if body is None:
                return done(status="skipped", skip_reason=SKIP_CANDIDATE_MISSING)
            model = deps.model_factory(DbPolicySource(session), org_id, cs)
        bundle = build_prompt(
            seed.question,
            list(seed.evidence),
            seed.history,
            budget_tokens=cs.CHAT_CONTEXT_TOKENS,
            reserve_output_tokens=cs.CHAT_MAX_OUTPUT_TOKENS,
            history_turns=cs.CHAT_HISTORY_TURNS,
            system_prompt=styles.compose(prompts.chat_system(body), seed.style),
        )
        if not bundle.should_call_model:
            return done(status="skipped", skip_reason=SKIP_NO_BUDGET)
        t0 = time.perf_counter()
        answer = call_with_timeout(
            lambda: model.complete(
                bundle.messages,
                temperature=cs.CHAT_TEMPERATURE,
                max_tokens=cs.CHAT_MAX_OUTPUT_TOKENS,
            ),
            cs.CHAT_GENERATION_TIMEOUT_SECONDS,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if not seed.still_valid():  # deleted / revoked while the candidate was writing
            return done(status="skipped", skip_reason=SKIP_EVIDENCE_DELETED)
        result = verify(
            answer,
            bundle.citation_map,
            bundle.used_evidence,
            config=VerifyConfig.from_settings(cs),
            embedder=deps.embedder,
        )
        validity = shadow.citation_validity(result.claims)
        done(
            status="completed",
            cand_status=result.status,
            cand_citation_validity=validity,
            cand_latency_ms=latency_ms,
            tokens_in=sum(estimate_tokens(m["content"]) for m in bundle.messages),
            tokens_out=estimate_tokens(answer),
            outcome=shadow.outcome(live.status, live.citation_validity, result.status, validity),
        )
    except Exception as exc:  # noqa: BLE001 - only the class name is kept, never any text
        log.warning("shadow row=%s failed (%s)", shadow_id, type(exc).__name__)
        done(status="failed", skip_reason=FAIL_ERROR)
