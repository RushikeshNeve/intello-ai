"""Research session execution (M07.02.01-.03; contract m07 section 4). No Celery in here.

``research.run`` (``app.workers.research_tasks``) claims the session (``research_claim_session``:
planned -> running, lease, fencing epoch) and keeps the lease alive; :class:`ResearchExecutor` does
the work of one claimed session. The work is a fixed list of persisted steps::

    0 plan | per sub-query: search, synthesize | last: report

* ``plan``: one local-model call (JSON, validated, <= the session's sub-query limit); the plan, the
  step list and the usage are written in ONE transaction.
* ``search``: ``gather`` + ``finish`` (PostgreSQL revalidation) + the graph hook; the evidence
  (ids, titles, passages) is stored in the step, capped by the evidence budget left.
* ``synthesize``: ``build_prompt`` + model + ``verify``; the kept, supported claims with their
  citations are stored in the step. A sub-query with no evidence makes no model call.
* ``report``: every statement's citations are resolved again NOW (``resolve_citation``) and a
  statement without a resolvable citation is withheld; citations are numbered across the report.

Resume: a step is ``done`` only after its result was written in the same transaction as its usage;
a restarted worker skips every ``done`` step (no repeated model call, no repeated step row: steps
are unique per ``(session, seq)``). Limits (sub-queries, evidence chunks, model calls, wall clock
across restarts) are enforced here; every write is fenced (``repo``). Failures end the session with
a stable code (``model_unavailable``, ``policy_denied``, ``search_unavailable``, ``timeout``,
``limit_exceeded``, ``internal_error``); no question, passage or answer text is ever logged.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import Engine

from app.core.config import Settings
from app.modules.ai import chat_repo
from app.modules.ai.chat import (
    ChatDeps,
    RunFailure,
    call_with_timeout,
    map_error,
    safe_error_message,
)
from app.modules.ai.context import build_prompt
from app.modules.ai.models.types import ChatModel
from app.modules.ai.policy_source import DbPolicySource
from app.modules.ai.structured import generate_structured
from app.modules.ai.verification import VerifyConfig, verify
from app.modules.graph.merge import augment_evidence
from app.modules.research import planner, repo
from app.modules.research.repo import ClaimedSession, SessionFence, SessionFenceLost, StepRow
from app.modules.retrieval import service as retrieval_service
from app.modules.retrieval.citations import CitationRef, resolve_citation
from app.modules.retrieval.scope import EmptyScope, RetrievalScope, scope_for_workspaces
from app.modules.usage.wiring import run_meter

log = logging.getLogger("ukos.research")

ERR_LIMIT_EXCEEDED: Final = "limit_exceeded"
LOCAL_LEASE_SAFETY: Final = 0.9
EXCERPT_MAX_CHARS: Final = 240
Outcome = Literal["completed", "failed", "fence_lost"]
_MARKER = re.compile(r"\s?\[\d{1,6}(?:\s*,\s*\d{1,6})*\]")


def error_message(code: str) -> str:
    if code == ERR_LIMIT_EXCEEDED:
        return "The research reached its configured limits and was stopped."
    return safe_error_message(code)


# ------------------------------------------------------------------------------------ heartbeat


class SessionHeartbeater:
    """Background lease renewal plus the cheap local fence (same semantics as the chat one)."""

    def __init__(
        self,
        engine: Engine,
        claim: ClaimedSession,
        *,
        lease_seconds: int,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._claim = claim
        self._lease = lease_seconds
        self._interval = interval_seconds
        self._clock = clock
        self._last_ok = clock()
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> SessionHeartbeater:
        thread = threading.Thread(
            target=self._loop, name=f"research-heartbeat-{self._claim.session_id}", daemon=True
        )
        self._thread = thread
        thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def check(self) -> None:
        if self._lost.is_set() or self._clock() - self._last_ok >= self._lease * LOCAL_LEASE_SAFETY:
            self._lost.set()
            raise SessionFenceLost(str(self._claim.session_id))

    def beat(self) -> None:
        self.check()
        try:
            with chat_repo.worker_chat_session(
                self._engine, org_id=self._claim.org_id, user_id=self._claim.created_by
            ) as session:
                repo.renew_lease(session, self._claim.fence, self._lease)
        except SessionFenceLost:
            self._lost.set()
            raise
        self._last_ok = self._clock()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.beat()
            except SessionFenceLost:
                log.warning("research session=%s: lease lost", self._claim.session_id)
                return
            except Exception as exc:  # noqa: BLE001 - keep trying until the local deadline
                log.warning(
                    "research session=%s heartbeat failed: %s",
                    self._claim.session_id,
                    type(exc).__name__,
                )


# --------------------------------------------------------------------------------- stored evidence


@dataclass(frozen=True)
class StoredEvidence:
    """Evidence kept in a ``search`` step (structural ``EvidenceLike`` for ``build_prompt``)."""

    evidence_id: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    text: str
    score: float
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> StoredEvidence:
        return cls(
            evidence_id=str(raw["evidence_id"]),
            chunk_id=uuid.UUID(str(raw["chunk_id"])),
            document_id=uuid.UUID(str(raw["document_id"])),
            version_id=uuid.UUID(str(raw["version_id"])),
            workspace_id=uuid.UUID(str(raw["workspace_id"])),
            title=str(raw.get("title", "")),
            text=str(raw.get("text", "")),
            score=float(raw.get("score", 0.0)),
            page_start=raw.get("page_start"),
            page_end=raw.get("page_end"),
            heading_path=tuple(raw.get("heading_path", ())),
        )


def _evidence_json(n: int, e: Any) -> dict[str, Any]:
    return {
        "evidence_id": f"e{n}",
        "chunk_id": str(e.chunk_id),
        "document_id": str(e.document_id),
        "version_id": str(e.version_id),
        "workspace_id": str(e.workspace_id),
        "title": e.title,
        "text": e.text,
        "score": float(e.score),
        "page_start": e.page_start,
        "page_end": e.page_end,
        "heading_path": list(e.heading_path),
    }


# --------------------------------------------------------------------------------- the executor


class ResearchExecutor:
    def __init__(
        self,
        *,
        engine: Engine,
        settings: Settings,
        deps: ChatDeps,
        claim: ClaimedSession,
        check: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.deps = deps
        self.claim = claim
        self.fence: SessionFence = claim.fence
        self._check = check
        self._clock = clock
        self._t0 = clock()
        self._deadline = 0.0
        self._model: ChatModel | None = None
        self._meter = run_meter(
            engine,
            org_id=claim.org_id,
            user_id=claim.created_by,
            run_id=claim.session_id,
            kind="research",
        )
        self._usage: dict[str, Any] = {}
        self._limits: dict[str, Any] = {}
        self._snapshot: frozenset[uuid.UUID] = frozenset()
        self._current_seq: int | None = None

    def _session(self) -> Any:
        return chat_repo.worker_chat_session(
            self.engine, org_id=self.claim.org_id, user_id=self.claim.created_by
        )

    def _remaining(self) -> float:
        return self._deadline - self._clock()

    # -- entry -----------------------------------------------------------------------------------

    def run(self) -> Outcome:
        try:
            return self._run()
        except SessionFenceLost:
            log.info("research session=%s: fenced out; stopping quietly", self.claim.session_id)
            return "fence_lost"
        except Exception as exc:  # noqa: BLE001 - every failure ends the session with a code
            code = (
                ERR_LIMIT_EXCEEDED
                if isinstance(exc, RunFailure) and exc.code == ERR_LIMIT_EXCEEDED
                else map_error(exc)
            )
            if not isinstance(exc, RunFailure):
                log.warning(
                    "research session=%s failed code=%s type=%s",
                    self.claim.session_id,
                    code,
                    type(exc).__name__,
                )
            return self._fail(code)

    def _fail(self, code: str) -> Outcome:
        try:
            with self._session() as session:
                if self._current_seq is not None:
                    repo.update_step(
                        session,
                        self.fence,
                        seq=self._current_seq,
                        state="failed",
                        result={"error_code": code},
                    )
                repo.finish_session(session, self.fence, state="failed", error_code=code)
        except SessionFenceLost:
            return "fence_lost"
        log.info("research session=%s failed code=%s", self.claim.session_id, code)
        return "failed"

    # -- the session -----------------------------------------------------------------------------

    def _run(self) -> Outcome:
        with self._session() as session:
            row, steps = repo.load_for_run(session, self.fence)
        self._limits = dict(row.limits)
        usage = dict(row.usage)
        self._usage = {
            "model_calls": int(usage.get("model_calls", 0)),
            "evidence_chunks": int(usage.get("evidence_chunks", 0)),
            "elapsed_seconds": float(usage.get("elapsed_seconds", 0.0)),
        }
        timeout = float(self._limits["timeout_seconds"])
        self._deadline = self._clock() + max(1.0, timeout - self._usage["elapsed_seconds"])
        ids = (
            row.scope_snapshot.get("workspace_ids")
            if isinstance(row.scope_snapshot, dict)
            else None
        )
        self._snapshot = frozenset(uuid.UUID(str(i)) for i in ids or [])
        by_seq = {s.seq: s for s in steps}

        plan_step = by_seq.get(0)
        if plan_step is None or plan_step.state != "done":
            self._plan(row.question)
            with self._session() as session:
                _, steps = repo.load_for_run(session, self.fence)
            by_seq = {s.seq: s for s in steps}

        for step in sorted(by_seq.values(), key=lambda s: s.seq):
            if step.kind == "plan" or step.state == "done":
                continue
            self._check()
            if self._remaining() <= 0:
                raise RunFailure("timeout")
            self._current_seq = step.seq
            with self._session() as session:
                repo.update_step(session, self.fence, seq=step.seq, state="running")
            if step.kind == "search":
                self._search(step)
            elif step.kind == "synthesize":
                self._synthesize(step, by_seq)
            else:
                return self._report(step, by_seq)
            by_seq[step.seq] = StepRow(step.seq, step.kind, step.query, "done", {})
        raise RunFailure("internal_error")  # a plan without a report step cannot happen

    # -- helpers ---------------------------------------------------------------------------------

    def _resolve_scope(self) -> RetrievalScope | None:
        """Workspaces the creator may read NOW and that the session was created with."""
        with self.engine.begin() as conn:
            readable = chat_repo.readable_workspaces(conn, self.claim.org_id, self.claim.created_by)
        allowed = readable & self._snapshot
        if not allowed:
            return None
        try:
            with self._session() as session:
                return scope_for_workspaces(
                    session,
                    org_id=self.claim.org_id,
                    user_id=self.claim.created_by,
                    workspace_ids=allowed,
                )
        except EmptyScope:
            return None

    def _commit(self, seq: int, result: dict[str, Any], session: Any) -> None:
        """Step done + usage, in the caller's transaction (so they land together)."""
        self._usage["elapsed_seconds"] = round(
            self._usage["elapsed_seconds"] + (self._clock() - self._t0), 3
        )
        self._t0 = self._clock()
        repo.update_step(session, self.fence, seq=seq, state="done", result=result)
        repo.set_plan_and_usage(session, self.fence, plan=None, usage=self._usage)

    def _open_model(self) -> ChatModel:
        if self._model is None:
            with self._session() as session:
                self._model = self._meter.chat(
                    self.deps.model_factory(
                        DbPolicySource(session), self.claim.org_id, self.deps.chat_settings
                    )
                )
        return self._model

    def _model_call(self, messages: Any, *, timeout: float) -> str:
        if self._usage["model_calls"] >= int(self._limits["max_model_calls"]):
            raise RunFailure(ERR_LIMIT_EXCEEDED)
        model = self._open_model()
        cs = self.deps.chat_settings
        return call_with_timeout(
            lambda: model.complete(
                messages, temperature=cs.CHAT_TEMPERATURE, max_tokens=cs.CHAT_MAX_OUTPUT_TOKENS
            ),
            timeout,
        )

    # -- plan ------------------------------------------------------------------------------------

    def _plan(self, question: str) -> None:
        budget = planner.subquery_budget(self._limits)
        self._current_seq = 0
        with self._session() as session:
            repo.insert_step(session, self.fence, seq=0, kind="plan", query=None)
            repo.update_step(session, self.fence, seq=0, state="running")
        self._check()
        timeout = min(
            self.deps.chat_settings.CHAT_GENERATION_TIMEOUT_SECONDS, max(1.0, self._remaining())
        )

        class _Counted:  # every call (plan or repair) is a counted model call
            def complete(inner, messages: Any, **_: Any) -> str:
                reply = self._model_call(messages, timeout=timeout)
                self._usage["model_calls"] += 1
                return reply

        # A repair must not starve the one synthesis call each sub-query still needs.
        spare = int(self._limits["max_model_calls"]) - self._usage["model_calls"] - 1 - budget
        result = generate_structured(
            _Counted(),
            planner.plan_messages(question, budget),
            planner.PlanReply,
            max_repairs=1 if spare >= 1 else 0,
        )
        subqueries = (result.value and planner.clean_plan(result.value, budget)) or [
            " ".join(question.split())[: planner.SUBQUERY_MAX_CHARS]
        ]
        with self._session() as session:
            for i, query in enumerate(subqueries):
                repo.insert_step(session, self.fence, seq=1 + 2 * i, kind="search", query=query)
                repo.insert_step(session, self.fence, seq=2 + 2 * i, kind="synthesize", query=query)
            repo.insert_step(
                session, self.fence, seq=1 + 2 * len(subqueries), kind="report", query=None
            )
            repo.set_plan_and_usage(
                session, self.fence, plan={"subqueries": subqueries}, usage=self._usage
            )
            self._commit(0, {"subqueries": subqueries}, session)
        self._current_seq = None

    # -- search ----------------------------------------------------------------------------------

    def _search(self, step: StepRow) -> None:
        scope = self._resolve_scope()
        left = int(self._limits["max_evidence"]) - self._usage["evidence_chunks"]
        evidence: list[Any] = []
        dropped = 0
        degraded: tuple[str, ...] = ()
        if scope is not None and left > 0 and step.query:
            s = self.settings
            query = step.query
            timeout = min(s.CHAT_RETRIEVAL_TIMEOUT_SECONDS, max(0.1, self._remaining()))
            try:
                gathered = call_with_timeout(
                    lambda: retrieval_service.gather(
                        scope,
                        self.deps.repo,
                        self._meter.embedder(self.deps.embedder),
                        query,
                        candidates=max(s.SEARCH_CANDIDATES, 1),
                        mode="hybrid",
                        max_per_document=s.SEARCH_MAX_PER_DOCUMENT,
                    ),
                    timeout,
                )
            except ValueError:  # not searchable after normalization: no evidence
                gathered = None
            self._check()
            if gathered is not None:
                with self._session() as session:
                    result = retrieval_service.finish(
                        session, scope, gathered, top_k=min(s.CHAT_EVIDENCE_TOP_K, left)
                    )
                evidence, dropped, degraded = list(result.evidence), result.dropped, result.degraded
                evidence = self._augment(scope, query, evidence)[:left]
        stored = [_evidence_json(n, e) for n, e in enumerate(evidence, start=1)]
        self._usage["evidence_chunks"] += len(stored)
        with self._session() as session:
            self._commit(
                step.seq,
                {
                    "evidence": stored,
                    "dropped": dropped,
                    "degraded": list(degraded),
                    "budget_exhausted": left <= 0,
                },
                session,
            )
        self._current_seq = None

    def _augment(self, scope: RetrievalScope, question: str, evidence: list[Any]) -> list[Any]:
        deps = self.deps
        if not self.settings.GRAPH_ENABLED or deps.graph_repo is None or deps.graph_chunks is None:
            return evidence
        repo_, chunks = deps.graph_repo, deps.graph_chunks
        try:

            def work() -> list[Any]:
                with self._session() as session:
                    return list(augment_evidence(session, scope, question, evidence, repo_, chunks))

            return call_with_timeout(work, min(5.0, max(0.1, self._remaining())))
        except Exception as exc:
            if isinstance(exc, SessionFenceLost):
                raise
            log.warning(
                "research session=%s: graph augmentation skipped (%s)",
                self.claim.session_id,
                type(exc).__name__,
            )
            return evidence

    # -- synthesize ------------------------------------------------------------------------------

    def _synthesize(self, step: StepRow, by_seq: dict[int, StepRow]) -> None:
        with self._session() as session:
            _, fresh = repo.load_for_run(session, self.fence)
        search = next((s for s in fresh if s.seq == step.seq - 1), None)
        raw = (search.result.get("evidence") if search is not None else None) or []
        evidence = [StoredEvidence.from_json(e) for e in raw]
        cs = self.deps.chat_settings
        result: dict[str, Any] = {
            "statements": [],
            "status": "abstained",
            "withheld_count": 0,
            "flagged_count": 0,
        }
        bundle = (
            build_prompt(
                step.query or "",
                evidence,
                (),
                budget_tokens=cs.CHAT_CONTEXT_TOKENS,
                reserve_output_tokens=cs.CHAT_MAX_OUTPUT_TOKENS,
                history_turns=0,
            )
            if evidence
            else None
        )
        if bundle is not None and bundle.should_call_model:
            text = self._model_call(
                bundle.messages,
                timeout=min(cs.CHAT_GENERATION_TIMEOUT_SECONDS, max(1.0, self._remaining())),
            )
            self._usage["model_calls"] += 1
            self._check()
            verification = verify(
                text,
                bundle.citation_map,
                bundle.used_evidence,
                config=VerifyConfig.from_settings(cs),
                embedder=self._meter.embedder(self.deps.embedder),
            )
            excerpts = {vc.marker: vc.excerpt for vc in verification.citations}
            statements: list[dict[str, Any]] = []
            unsourced = 0
            for claim in verification.claims:
                if not (claim.kept and claim.status == "supported"):
                    continue
                body = _MARKER.sub("", claim.text).strip()
                cites = []
                for n in claim.supporting_markers:
                    if not 1 <= n <= len(bundle.used_evidence):
                        continue
                    e = bundle.used_evidence[n - 1]
                    cites.append(
                        {
                            "chunk_id": str(e.chunk_id),
                            "document_id": str(e.document_id),
                            "version_id": str(e.version_id),
                            "workspace_id": str(getattr(e, "workspace_id", "")),
                            "page_start": e.page_start,
                            "page_end": e.page_end,
                            "excerpt": (excerpts.get(f"[{n}]") or e.text)[:EXCERPT_MAX_CHARS],
                        }
                    )
                if body and cites:
                    statements.append({"text": body, "citations": cites})
                else:
                    unsourced += 1
            result = {
                "statements": statements,
                "status": verification.status,
                "withheld_count": len(verification.withheld) + unsourced,
                "flagged_count": len(verification.flagged),
            }
        with self._session() as session:
            self._commit(step.seq, result, session)
        self._current_seq = None

    # -- report ----------------------------------------------------------------------------------

    def _report(self, step: StepRow, by_seq: dict[int, StepRow]) -> Outcome:
        with self._session() as session:
            _, fresh = repo.load_for_run(session, self.fence)
        scope = self._resolve_scope()
        numbers: dict[str, int] = {}
        sections: list[dict[str, Any]] = []
        kept = withheld = flagged = 0
        resolved_cache: dict[tuple[str, str, str], bool] = {}
        with self._session() as session:
            for synth in (s for s in fresh if s.kind == "synthesize"):
                withheld += int(synth.result.get("withheld_count", 0))
                flagged += int(synth.result.get("flagged_count", 0))
                statements: list[dict[str, Any]] = []
                for st in synth.result.get("statements", []):
                    cites: list[dict[str, Any]] = []
                    for c in st.get("citations", []):
                        key = (str(c["chunk_id"]), str(c["document_id"]), str(c["version_id"]))
                        if key not in resolved_cache:
                            resolved_cache[key] = scope is not None and (
                                resolve_citation(
                                    session,
                                    scope,
                                    CitationRef(
                                        chunk_id=uuid.UUID(key[0]),
                                        document_id=uuid.UUID(key[1]),
                                        version_id=uuid.UUID(key[2]),
                                    ),
                                )
                                is not None
                            )
                        if not resolved_cache[key]:
                            continue
                        n = numbers.setdefault(key[0], len(numbers) + 1)
                        cites.append({**c, "marker": f"[{n}]"})
                    if cites:
                        statements.append({"text": st["text"], "citations": cites})
                        kept += 1
                    else:
                        withheld += 1
                sections.append({"heading": synth.query or "", "statements": statements})
        status = "abstained" if kept == 0 else ("partial" if withheld or flagged else "grounded")
        verification_out = {
            "status": status,
            "statements": kept,
            "withheld_count": withheld,
            "flagged_count": flagged,
            "subqueries": len(sections),
        }
        with self._session() as session:
            repo.insert_report(
                session, self.fence, sections=sections, verification=verification_out
            )
            self._commit(step.seq, {"statements": kept}, session)
            repo.finish_session(session, self.fence, state="completed")
        self._current_seq = None
        log.info(
            "research session=%s completed status=%s statements=%d withheld=%d",
            self.claim.session_id,
            status,
            kept,
            withheld,
        )
        return "completed"
