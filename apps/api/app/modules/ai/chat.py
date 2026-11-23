"""Chat run execution (M04.02.04; contract m04 §8 and §14). Pure orchestration, no Celery.

The ``chat.run`` task (``app.workers.chat_tasks``) claims the run and keeps its lease alive; this
module does the work of one claimed run and is what the tests drive directly:

``started`` -> scope re-resolution (readable now AND in the creation snapshot) -> retrieval
(``gather`` with a timeout, then ``finish`` = PostgreSQL revalidation) -> ``retrieval`` -> no
evidence: fixed abstention, the model is never called -> else prompt, policy-checked model,
streamed ``delta`` events (coalesced: 80 ms or 40 characters) -> ``verify`` on the full text ->
citations re-resolved against PostgreSQL (a document deleted meanwhile is not cited) ->
``citations``, ``verification``, assistant message (kept text only), ``completed``.

Failure rules (stable codes, user-safe messages, never internals or content):

* ``policy_denied``   the gateway refused every provider for the org;
* ``search_unavailable``  the index/embedder failed (never an "empty result");
* ``model_unavailable``   any chat-model error (the gateway already retried transient failures
  before the first token; after the first token nothing is retried, a restart would duplicate
  text); the partial text streamed so far stays in the event log and the run ends ``failed``;
* ``timeout``         retrieval (10 s), generation, or the whole run exceeded its budget;
* ``internal_error``  anything else (only the exception class name is logged);
* ``lease_expired``   written by the reaper, not here (a worker that died). A run with no ``delta``
  yet is requeued once instead (``chat_reap_expired_runs``); events already written are then not
  repeated, so a requeued run never duplicates an event.

Every write is fenced (``chat_repo``): a worker that lost its lease or whose run was cancelled gets
:class:`~app.modules.ai.chat_repo.RunFenceLost` and stops quietly without writing anything more.
No question, evidence or answer text is ever logged or put into audit rows.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from sqlalchemy import Engine, select

from app.core.config import Settings
from app.modules import prompts
from app.modules.ai import chat_repo
from app.modules.ai.chat_repo import ClaimedRun, RunFence, RunFenceLost
from app.modules.ai.context import HistoryTurn, PromptBundle, build_prompt
from app.modules.ai.embeddings import EmbeddingProvider
from app.modules.ai.models.errors import ChatModelError
from app.modules.ai.models.policy import PolicyDenied, PolicySource
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import ChatModel
from app.modules.ai.models_db import ChatMessageRow, ChatRun
from app.modules.ai.policy_source import DbPolicySource
from app.modules.ai.prompts import PROMPT_VERSION
from app.modules.ai.verification import (
    Verification,
    VerifiedCitation,
    VerifyConfig,
    abstention,
    verify,
)
from app.modules.firewall import hooks as firewall_hooks
from app.modules.firewall import policy as firewall_policy
from app.modules.firewall.output_guard import Context, OutputBlocked, StreamGuard
from app.modules.firewall import abuse
from app.modules.graph.merge import augment_evidence
from app.modules.graph.ports_read import ChunkSource, GraphReadRepository
from app.modules.knowledge.ports import KnowledgeRepository
from app.modules.prompts import styles
from app.modules.retrieval import service as retrieval_service
from app.modules.retrieval.citations import CitationRef, ResolvedCitation, resolve_citation
from app.modules.retrieval.hybrid import SearchUnavailable
from app.modules.retrieval.scope import EmptyScope, RetrievalScope, scope_for_workspaces
from app.modules.usage.limits import QUOTA_MESSAGE, QuotaExceeded
from app.modules.usage.wiring import run_meter

log = logging.getLogger("ukos.chat")

# -- public vocabulary -----------------------------------------------------------------------

STATE_QUEUED: Final = "queued"
STATE_RUNNING: Final = "running"
STATE_COMPLETED: Final = "completed"
STATE_FAILED: Final = "failed"
STATE_CANCELLED: Final = "cancelled"
TERMINAL_STATES: Final = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED})
# Event types that end a run's stream.
TERMINAL_EVENT_TYPES: Final = frozenset({"completed", "failed", "cancelled"})

ERR_MODEL_UNAVAILABLE: Final = "model_unavailable"
ERR_POLICY_DENIED: Final = "policy_denied"
ERR_SEARCH_UNAVAILABLE: Final = "search_unavailable"
ERR_TIMEOUT: Final = "timeout"
ERR_INTERNAL: Final = "internal_error"
ERR_LEASE_EXPIRED: Final = "lease_expired"
ERR_QUOTA_EXCEEDED: Final = "quota_exceeded"  # M11.08
ERR_INPUT_BLOCKED: Final = "input_blocked"  # M14.01
ERR_OUTPUT_BLOCKED: Final = "output_blocked"  # M14.03
ERR_PII_BLOCKED: Final = "pii_blocked"  # M14.04

ERROR_MESSAGES: Final[Mapping[str, str]] = {
    ERR_MODEL_UNAVAILABLE: "The AI model is not available right now. Please try again.",
    ERR_POLICY_DENIED: "Your organization's AI policy does not allow answering right now.",
    ERR_SEARCH_UNAVAILABLE: "Search is not available right now. Please try again.",
    ERR_TIMEOUT: "The answer took too long and was stopped. Please try again.",
    ERR_INTERNAL: "Something went wrong while answering. Please try again.",
    ERR_LEASE_EXPIRED: "The answer was interrupted. Please try again.",
    ERR_QUOTA_EXCEEDED: QUOTA_MESSAGE,
    ERR_INPUT_BLOCKED: "This message cannot be processed because it looks like an attempt to "
    "change how the assistant works. Please rephrase your question.",
    ERR_OUTPUT_BLOCKED: "The answer was withheld by your organization's AI safety policy.",
    ERR_PII_BLOCKED: "The question contains sensitive data that cannot be sent to the AI provider.",
}


_MARKER_RE = re.compile(r"\s?\[\d{1,3}\]")


def strip_markers(text: str) -> str:
    """Citation markers of unverified text: dropped, so nothing looks cited."""
    return _MARKER_RE.sub("", text).strip() or text.strip()


def safe_error_message(code: str) -> str:
    """The user-safe text of an error code (unknown codes read as ``internal_error``)."""
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES[ERR_INTERNAL])


# Streaming/coalescing (contract §8): a delta event per 80 ms or 40 characters, whichever first.
COALESCE_SECONDS: Final = 0.08
COALESCE_CHARS: Final = 40
EXCERPT_MAX_CHARS: Final = 240
RETRIEVAL_CANDIDATES_FLOOR: Final = 1
LOCAL_LEASE_SAFETY: Final = 0.9
_IDLE_POLL_SECONDS: Final = 0.5

RunOutcome = Literal["completed", "failed", "fence_lost"]


class RunFailure(Exception):
    """End the run ``failed`` with this stable code (the message is derived from it)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def map_error(exc: BaseException) -> str:
    """Stable error code of an exception raised while executing a run (never the message)."""
    if isinstance(exc, RunFailure):
        return exc.code
    if isinstance(exc, QuotaExceeded):
        return ERR_QUOTA_EXCEEDED
    if isinstance(exc, PolicyDenied):
        return ERR_POLICY_DENIED
    if isinstance(exc, SearchUnavailable):
        return ERR_SEARCH_UNAVAILABLE
    if getattr(exc, "code", None) == "pii_blocked" and isinstance(exc, ChatModelError):
        return ERR_PII_BLOCKED
    if isinstance(exc, ChatModelError):
        return ERR_MODEL_UNAVAILABLE
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "SoftTimeLimitExceeded":
        return ERR_TIMEOUT
    return ERR_INTERNAL


# -- coalescer ------------------------------------------------------------------------------


class DeltaCoalescer:
    """Buffers model deltas and releases them as one text when 40 characters are buffered or 80 ms
    passed since the last release (the first delta is released at once: low first-token latency)."""

    def __init__(
        self,
        *,
        min_interval: float = COALESCE_SECONDS,
        min_chars: int = COALESCE_CHARS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = min_interval
        self._chars = min_chars
        self._clock = clock
        self._buf: list[str] = []
        self._size = 0
        self._last = clock() - min_interval

    @property
    def pending(self) -> bool:
        return self._size > 0

    def add(self, text: str) -> str | None:
        if not text:
            return None
        self._buf.append(text)
        self._size += len(text)
        return self.due()

    def due(self) -> str | None:
        if self._size == 0:
            return None
        if self._size >= self._chars or self._clock() - self._last >= self._interval:
            return self.flush()
        return None

    def time_to_due(self) -> float:
        """Seconds until buffered text is due (0 when due now); meaningless without ``pending``."""
        return max(0.0, self._interval - (self._clock() - self._last))

    def flush(self) -> str | None:
        if self._size == 0:
            return None
        text = "".join(self._buf)
        self._buf.clear()
        self._size = 0
        self._last = self._clock()
        return text


# -- helpers: timeout call, model pump ------------------------------------------------------


def call_with_timeout[T](fn: Callable[[], T], seconds: float) -> T:
    """Run ``fn`` in a daemon thread; ``TimeoutError`` when it does not finish in ``seconds``
    (the thread is abandoned: it only does network I/O that has its own timeouts)."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=target, name="chat-timeout-call", daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    if thread.is_alive():
        raise TimeoutError("call timed out")
    if "error" in box:
        raise box["error"]
    value: T = box["value"]
    return value


class ModelPump:
    """Iterates a model's delta stream in a daemon thread so the consumer can poll with a timeout
    (flush coalesced text while the model is quiet, notice deadlines and a lost lease)."""

    def __init__(self, open_stream: Callable[[], Iterator[str]]) -> None:
        self._open = open_stream
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, name="chat-model-pump", daemon=True)

    def start(self) -> ModelPump:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def get(self, timeout: float) -> tuple[str, Any]:
        """``("delta", text)``, ``("done", None)``, ``("error", exc)`` or ``("idle", None)``."""
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return ("idle", None)

    def _pump(self) -> None:
        stream: Iterator[str] | None = None
        try:
            stream = iter(self._open())
            for piece in stream:
                if self._stop.is_set():
                    return
                self._queue.put(("delta", piece))
            self._queue.put(("done", None))
        except BaseException as exc:  # noqa: BLE001 - handed to the consumer
            self._queue.put(("error", exc))
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):  # closing a dead stream is best effort
                    close()


# -- heartbeat ------------------------------------------------------------------------------


class RunHeartbeater:
    """Background lease renewal plus the cheap local fence (same semantics as the M03 job one).

    ``check()`` raises :class:`RunFenceLost` when a renewal was rejected (lease gone, run
    cancelled or finished) or when none succeeded for 90% of a lease period.
    """

    def __init__(
        self,
        engine: Engine,
        claim: ClaimedRun,
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

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> RunHeartbeater:
        thread = threading.Thread(
            target=self._loop, name=f"chat-heartbeat-{self._claim.run_id}", daemon=True
        )
        self._thread = thread
        thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def check(self) -> None:
        if self._lost.is_set():
            raise RunFenceLost(str(self._claim.run_id))
        if self._clock() - self._last_ok >= self._lease * LOCAL_LEASE_SAFETY:
            self._lost.set()
            raise RunFenceLost(str(self._claim.run_id))

    def beat(self) -> None:
        self.check()
        try:
            with chat_repo.worker_chat_session(
                self._engine, org_id=self._claim.org_id, user_id=self._claim.created_by
            ) as session:
                chat_repo.renew_lease(session, self._claim.fence, self._lease)
        except RunFenceLost:
            self._lost.set()
            raise
        self._last_ok = self._clock()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.beat()
            except RunFenceLost:
                log.warning(
                    "chat run=%s epoch=%s: lease lost", self._claim.run_id, self._claim.lease_epoch
                )
                return
            except Exception as exc:  # noqa: BLE001 - keep trying until the local deadline
                log.warning(
                    "chat run=%s heartbeat failed: %s", self._claim.run_id, type(exc).__name__
                )


# -- dependencies ---------------------------------------------------------------------------

ModelFactory = Callable[[PolicySource, uuid.UUID, ChatSettings], ChatModel]


def default_model_factory(
    policy_source: PolicySource, org_id: uuid.UUID, settings: ChatSettings
) -> ChatModel:
    from app.modules.ai.gateway import get_chat_model

    return get_chat_model(policy_source, org_id, settings)


@dataclass(frozen=True)
class ChatDeps:
    """What a run needs besides the database: the knowledge index, the embedder (``None`` =
    keyword-only retrieval and no embedding check), the chat settings and the model factory (the
    gateway's ``get_chat_model``, the single policy choke point; tests substitute it)."""

    repo: KnowledgeRepository
    embedder: EmbeddingProvider | None
    chat_settings: ChatSettings = field(default_factory=ChatSettings)
    model_factory: ModelFactory = default_model_factory
    # M05 graph-augmented evidence: used only when ``Settings.GRAPH_ENABLED`` and both are set.
    graph_repo: GraphReadRepository | None = None
    graph_chunks: ChunkSource | None = None


@dataclass(frozen=True)
class ShadowSeed:
    """In-memory hand-over of a completed live run to a prompt shadow run (M12.09): the SAME
    evidence and question, the live outcome as numbers, and a re-check that every evidence passage
    still resolves for the run's creator. Never persisted."""

    question: str
    history: tuple[HistoryTurn, ...]
    evidence: tuple[Any, ...]
    verification: Verification
    latency_ms: int
    prompt_version_id: uuid.UUID | None
    still_valid: Callable[[], bool]
    # M12.07: the live answer style, so the candidate is composed exactly like the live prompt.
    style: styles.Style | None = None


# -- the executor ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunInput:
    question: str
    history: tuple[HistoryTurn, ...]
    snapshot_workspaces: frozenset[uuid.UUID]
    existing_event_types: frozenset[str]


def _uuids(values: object) -> frozenset[uuid.UUID]:
    out: set[uuid.UUID] = set()
    if isinstance(values, list):
        for v in values:
            try:
                out.add(uuid.UUID(str(v)))
            except ValueError:
                continue
    return frozenset(out)


def citation_payload(vc: VerifiedCitation, resolved: ResolvedCitation) -> dict[str, Any]:
    """The pinned ``Citation`` JSON (contract §14); excerpt <= 240 characters from the chunk."""
    return {
        "marker": vc.marker,
        "chunk_id": vc.chunk_id,
        "document_id": vc.document_id,
        "version_id": vc.version_id,
        "workspace_id": str(resolved.workspace_id),
        "title": resolved.title,
        "version_number": resolved.version_number,
        "page_start": vc.page_start,
        "page_end": vc.page_end,
        "excerpt": vc.excerpt[:EXCERPT_MAX_CHARS],
    }


def verification_payload(v: Verification) -> dict[str, Any]:
    return {
        "status": v.status,
        "withheld_count": len(v.withheld),
        "flagged_count": len(v.flagged),
    }


def fail_run(engine: Engine, claim: ClaimedRun, code: str, pending: str = "") -> RunOutcome:
    """End a claimed run ``failed`` with a stable code (``pending`` = model text still buffered,
    written first so the log holds every character the user may have seen). Fenced: a worker that
    lost the run writes nothing and returns ``fence_lost``."""
    try:
        with chat_repo.worker_chat_session(
            engine, org_id=claim.org_id, user_id=claim.created_by
        ) as session:
            if pending:
                chat_repo.append_event(session, claim.fence, type="delta", data={"text": pending})
            chat_repo.append_event(
                session,
                claim.fence,
                type="failed",
                data={"error_code": code, "message": safe_error_message(code)},
            )
            chat_repo.finish_run(session, claim.fence, state=STATE_FAILED, error_code=code)
    except RunFenceLost:
        log.info("chat run=%s: fenced out while failing; stopping quietly", claim.run_id)
        return "fence_lost"
    log.info("chat run=%s failed code=%s", claim.run_id, code)
    return "failed"


class RunExecutor:
    """Executes one claimed run. ``check`` is the local fence (heartbeater): it raises
    :class:`RunFenceLost` once the lease is gone."""

    def __init__(
        self,
        *,
        engine: Engine,
        settings: Settings,
        deps: ChatDeps,
        claim: ClaimedRun,
        check: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.deps = deps
        self.claim = claim
        self.fence: RunFence = claim.fence
        self._check = check
        self._clock = clock
        self._deadline = clock() + settings.CHAT_RUN_TIMEOUT_SECONDS
        self._last_probe = clock()
        self._partial = ""  # all model text received so far (for a timeout answer)
        self._pending = ""  # buffered model text not yet written when a failure interrupts
        self._meter = run_meter(
            engine, org_id=claim.org_id, user_id=claim.created_by, run_id=claim.run_id, kind="chat"
        )
        self._prompt = prompts.Resolved(
            prompts.default_body("chat_system"),
            None,
            prompts.sha256_of(prompts.default_body("chat_system")),
        )
        self.shadow_seed: ShadowSeed | None = None  # set when a model-answered run completes
        self._seed_parts: tuple[str, tuple[HistoryTurn, ...], tuple[Any, ...], int] | None = None
        self._style: styles.Style | None = None
        self._fw = firewall_policy.FirewallPolicy()  # M14: default off
        self._fw_meta: dict[str, Any] = {}  # counts and rule ids only, never text
        self._guard: StreamGuard | None = None
        self._model_meta: dict[str, Any] = {}

    # -- plumbing ----------------------------------------------------------------------------

    def _session(self) -> Any:
        return chat_repo.worker_chat_session(
            self.engine, org_id=self.claim.org_id, user_id=self.claim.created_by
        )

    def _event(self, type_: str, data: Mapping[str, Any] | None = None) -> int:
        self._check()
        with self._session() as session:
            return chat_repo.append_event(session, self.fence, type=type_, data=data)

    def _remaining(self) -> float:
        return self._deadline - self._clock()

    # -- entry -------------------------------------------------------------------------------

    def run(self) -> RunOutcome:
        try:
            return self._run()
        except RunFenceLost:
            log.info("chat run=%s: fenced out; stopping quietly", self.claim.run_id)
            return "fence_lost"
        except Exception as exc:  # noqa: BLE001 - every failure ends the run with a stable code
            code = map_error(exc)
            if not isinstance(exc, RunFailure):
                log.warning(
                    "chat run=%s failed code=%s type=%s",
                    self.claim.run_id,
                    code,
                    type(exc).__name__,
                )
            if code == ERR_TIMEOUT and (self._partial or self._pending).strip():
                return self._finish_unverified("timeout")
            return self._fail(code, self._pending)

    def _finish_unverified(self, reason: str) -> RunOutcome:
        """Model timeout after text was streamed: keep the partial answer, completed but labelled
        ``unverified`` (``stopped: timeout``), with no citations (nothing was verified)."""
        text = strip_markers(self._partial)
        try:
            with self._session() as session:
                if self._pending:
                    chat_repo.append_event(
                        session, self.fence, type="delta", data={"text": self._pending}
                    )
                vp = {
                    "status": "unverified",
                    "withheld_count": 0,
                    "flagged_count": 0,
                    "stopped": reason,
                }
                chat_repo.append_event(
                    session, self.fence, type="citations", data={"citations": []}
                )
                chat_repo.append_event(session, self.fence, type="verification", data=vp)
                message_id = chat_repo.append_assistant_message(
                    session,
                    self.fence,
                    thread_id=self.claim.thread_id,
                    content=text,
                    citations=[],
                    verification=vp,
                )
                chat_repo.append_event(
                    session,
                    self.fence,
                    type="completed",
                    data={"message_id": str(message_id), "answer_text": text},
                )
                chat_repo.finish_run(session, self.fence, state=STATE_COMPLETED)
        except RunFenceLost:
            return "fence_lost"
        log.info("chat run=%s completed unverified (%s)", self.claim.run_id, reason)
        return "completed"

    def _fail(self, code: str, pending: str = "") -> RunOutcome:
        return fail_run(self.engine, self.claim, code, pending)

    # -- the run -----------------------------------------------------------------------------

    def _load_input(self) -> RunInput:
        keep = max(self.deps.chat_settings.CHAT_HISTORY_TURNS, 1) * 2 + 2
        with self._session() as session:
            snapshot = session.execute(
                select(ChatRun.scope_snapshot).where(ChatRun.id == self.claim.run_id)
            ).scalar_one()
            current = session.execute(
                select(ChatMessageRow.content, ChatMessageRow.created_at).where(
                    ChatMessageRow.id == self.claim.user_message_id
                )
            ).one()
            prior = session.execute(
                select(ChatMessageRow.role, ChatMessageRow.content)
                .where(
                    ChatMessageRow.thread_id == self.claim.thread_id,
                    ChatMessageRow.id != self.claim.user_message_id,
                    ChatMessageRow.created_at < current.created_at,
                )
                .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc())
                .limit(keep)
            ).all()
            events = chat_repo.read_events_after(
                session, org_id=self.claim.org_id, run_id=self.claim.run_id, after_seq=0, limit=1000
            )
        history = tuple(
            HistoryTurn("assistant" if role == "assistant" else "user", content)
            for role, content in reversed(prior)
        )
        ids = snapshot.get("workspace_ids") if isinstance(snapshot, dict) else None
        return RunInput(
            question=current.content,
            history=history,
            snapshot_workspaces=_uuids(ids),
            existing_event_types=frozenset(e.type for e in events),
        )

    def _resolve_scope(self, snapshot: frozenset[uuid.UUID]) -> RetrievalScope | None:
        """Workspaces the creator may read NOW and that the run was created with (a revoked
        workspace stops contributing). ``None`` = nothing readable."""
        with self.engine.begin() as conn:
            readable = chat_repo.readable_workspaces(conn, self.claim.org_id, self.claim.created_by)
        allowed = readable & snapshot
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

    def _retrieve(
        self, scope: RetrievalScope | None, question: str
    ) -> retrieval_service.RetrievalResult:
        if scope is None:
            return retrieval_service.RetrievalResult([], 0, (), 0)
        s = self.settings
        timeout = min(s.CHAT_RETRIEVAL_TIMEOUT_SECONDS, max(0.1, self._remaining()))
        try:
            gathered = call_with_timeout(
                lambda: retrieval_service.gather(
                    scope,
                    self.deps.repo,
                    self._meter.embedder(self.deps.embedder),
                    question,
                    candidates=max(s.SEARCH_CANDIDATES, RETRIEVAL_CANDIDATES_FLOOR),
                    mode="hybrid",
                    max_per_document=s.SEARCH_MAX_PER_DOCUMENT,
                ),
                timeout,
            )
        except ValueError:  # the question is not searchable after normalization: no evidence
            return retrieval_service.RetrievalResult([], 0, (), 0)
        self._check()
        with self._session() as session:
            return retrieval_service.finish(session, scope, gathered, top_k=s.CHAT_EVIDENCE_TOP_K)

    def _augment(
        self, scope: RetrievalScope | None, question: str, evidence: list[Any]
    ) -> list[Any]:
        """Graph-found original chunks after the base evidence; any failure keeps the base."""
        deps = self.deps
        if (
            not self.settings.GRAPH_ENABLED
            or scope is None
            or deps.graph_repo is None
            or deps.graph_chunks is None
        ):
            return evidence
        repo, chunks = deps.graph_repo, deps.graph_chunks
        try:
            timeout = min(5.0, max(0.1, self._remaining()))

            def work() -> list[Any]:
                with self._session() as session:
                    return list(augment_evidence(session, scope, question, evidence, repo, chunks))

            return call_with_timeout(work, timeout)
        except Exception as exc:
            if isinstance(exc, RunFenceLost):
                raise
            log.warning(
                "chat run=%s: graph augmentation skipped (%s)",
                self.claim.run_id,
                type(exc).__name__,
            )
            return evidence

    def _with_memory(
        self, scope: RetrievalScope | None, question: str, evidence: list[Any]
    ) -> list[Any]:
        """M07: approved, valid memories as extra cited evidence; any failure keeps the base."""
        if not getattr(self.settings, "MEMORY_ENABLED", False) or scope is None:
            return evidence
        try:
            from app.modules.memory.chat import memory_evidence

            with self._session() as session:
                extra = memory_evidence(
                    session,
                    scope,
                    question,
                    evidence,
                    limit=self.settings.MEMORY_MAX_PER_PROMPT,
                )
            return [*evidence, *extra]
        except Exception as exc:
            if isinstance(exc, RunFenceLost):
                raise
            log.warning("chat run=%s: memory skipped (%s)", self.claim.run_id, type(exc).__name__)
            return evidence

    def _run(self) -> RunOutcome:
        data = self._load_input()
        existing = data.existing_event_types
        if "started" not in existing:
            self._event("started", {})
        self._fw = firewall_hooks.load_policy(self._session, self.claim.org_id)
        if firewall_hooks.check_user(self._fw, data.question, self._fw_meta):
            self._record_firewall()
            raise RunFailure(ERR_INPUT_BLOCKED)
        scope = self._resolve_scope(data.snapshot_workspaces)
        result = self._retrieve(scope, data.question)
        evidence = self._augment(scope, data.question, list(result.evidence))
        evidence = self._with_memory(scope, data.question, evidence)
        abuse.canary_check(
            lambda *, user_id, org_id: chat_repo.worker_chat_session(
                self.engine,
                org_id=org_id,
                user_id=user_id,  # type: ignore[arg-type]
            ),
            self.claim.org_id,
            self.claim.created_by,
            [e.document_id for e in evidence],
            run_id=self.claim.run_id,
        )
        evidence = firewall_hooks.filter_evidence(self._fw, evidence, self._fw_meta)
        if "retrieval" not in existing:
            self._event(
                "retrieval",
                {
                    "evidence_count": len(evidence),
                    "dropped": result.dropped,
                    "degraded": list(result.degraded),
                },
            )
        cs = self.deps.chat_settings
        with self._session() as session:
            self._prompt = prompts.resolve("chat_system", self.claim.org_id, session)
            style = (
                None
                if scope is None
                else styles.resolve_style(session, self.claim.org_id, scope.workspace_ids)
            )
            self._style = style
        system_prompt = firewall_hooks.with_canary(
            self._fw,
            self.claim.org_id,
            styles.compose(prompts.chat_system(self._prompt.body), style),
        )
        bundle = (
            build_prompt(
                data.question,
                evidence,
                data.history,
                budget_tokens=cs.CHAT_CONTEXT_TOKENS,
                reserve_output_tokens=cs.CHAT_MAX_OUTPUT_TOKENS,
                history_turns=cs.CHAT_HISTORY_TURNS,
                system_prompt=system_prompt,
            )
            if evidence
            else None
        )
        if bundle is None or not bundle.should_call_model or scope is None:
            self._record_firewall()
            return self._complete(abstention("no_evidence", cs.VERIFY_MODE), [])

        self._guard = firewall_hooks.output_guard(
            self._fw, self.claim.org_id, system_prompt, bundle
        )
        model = self._open_model()
        t_gen = self._clock()
        text = self._generate(model, bundle)
        self._seed_parts = (
            data.question,
            data.history,
            tuple(evidence),
            int((self._clock() - t_gen) * 1000),
        )
        return self._finalize(text, bundle, scope)

    def _open_model(self) -> ChatModel:
        cs = self.deps.chat_settings
        with self._session() as session:
            model = self.deps.model_factory(DbPolicySource(session), self.claim.org_id, cs)
            self._model_meta = {
                "provider": model.spec.provider,
                "model_id": model.spec.model_id,
                "prompt_version": PROMPT_VERSION,
                # M12.01: which registry version produced the system text (None = repo file).
                "prompt_version_id": (
                    None if self._prompt.version_id is None else str(self._prompt.version_id)
                ),
                "prompt_sha256": self._prompt.sha256,
                **({"firewall": self._fw_meta} if self._fw_meta else {}),
            }
            chat_repo.set_run_model(session, self.fence, self._model_meta)
        return self._meter.chat(model)

    def _record_firewall(self) -> None:
        """M14: persist the firewall decisions (counts, rule ids) in the run's model metadata."""
        if not self._fw_meta:
            return
        self._model_meta = {**self._model_meta, "firewall": self._fw_meta}
        try:
            with self._session() as session:
                chat_repo.set_run_model(session, self.fence, self._model_meta)
        except RunFenceLost:
            raise
        except Exception:  # noqa: BLE001 - metadata must never break the run
            log.warning("chat run=%s firewall metadata not written", self.claim.run_id)

    def _generate(self, model: ChatModel, bundle: PromptBundle) -> str:
        cs = self.deps.chat_settings
        deadline = min(self._deadline, self._clock() + cs.CHAT_GENERATION_TIMEOUT_SECONDS)
        coalescer = DeltaCoalescer(clock=self._clock)
        pump = ModelPump(
            lambda: model.stream(
                bundle.messages,
                temperature=cs.CHAT_TEMPERATURE,
                max_tokens=cs.CHAT_MAX_OUTPUT_TOKENS,
            )
        ).start()
        pieces: list[str] = []
        guard = self._guard or StreamGuard(firewall_policy.OFF, Context())
        try:
            while True:
                now = self._clock()
                if now >= deadline:
                    raise RunFailure(ERR_TIMEOUT)
                idle = coalescer.time_to_due() if coalescer.pending else _IDLE_POLL_SECONDS
                kind, value = pump.get(min(deadline - now, idle))
                out: str | None = None
                if kind == "delta":
                    safe = guard.feed(value)  # M14.03: held back until the guard clears it
                    if safe:
                        pieces.append(safe)
                        self._partial += safe
                        out = coalescer.add(safe)
                elif kind == "done":
                    break
                elif kind == "error":
                    raise value
                else:
                    out = coalescer.due()
                    self._probe_cancel()
                if out:
                    self._event("delta", {"text": out})
                self._check()
            tail = guard.finish()
            if tail:
                pieces.append(tail)
                self._partial += tail
                out = coalescer.add(tail)
                if out:
                    self._event("delta", {"text": out})
        except RunFenceLost:
            raise
        except OutputBlocked as blocked:
            self._pending = coalescer.flush() or ""
            self._fw_meta.update(guard.summary())
            log.info("chat run=%s output blocked rules=%s", self.claim.run_id, blocked.rule_ids)
            self._record_firewall()
            raise RunFailure(ERR_OUTPUT_BLOCKED) from None
        except BaseException:
            # Failing after text was streamed: what is still buffered is written together with the
            # ``failed`` event, so the log holds every character the user may have seen.
            self._pending = coalescer.flush() or ""
            raise
        finally:
            pump.stop()
        rest = coalescer.flush()
        if rest:
            self._event("delta", {"text": rest})
        if guard.mode != firewall_policy.OFF:
            self._fw_meta.update(guard.summary())
        return "".join(pieces)

    def _probe_cancel(self) -> None:
        """While the model is silent no event is written, so look at the run now and then: a
        cancelled run stops generating within about a second (RunFenceLost)."""
        now = self._clock()
        if now - self._last_probe < 1.0:
            return
        self._last_probe = now
        with self._session() as session:
            if not chat_repo.run_still_running(session, self.fence):
                raise RunFenceLost(str(self.claim.run_id))

    def _finalize(self, text: str, bundle: PromptBundle, scope: RetrievalScope) -> RunOutcome:
        cs = self.deps.chat_settings
        # Documents may have been deleted, or access revoked, while the model was writing: resolve
        # every context passage again against PostgreSQL and cite only what still resolves.
        fresh = self._resolve_scope(scope.workspace_ids)
        resolved: dict[str, ResolvedCitation] = {}
        if fresh is not None:
            with self._session() as session:
                for e in bundle.used_evidence:
                    ref = CitationRef(
                        chunk_id=uuid.UUID(str(e.chunk_id)),
                        document_id=uuid.UUID(str(e.document_id)),
                        version_id=uuid.UUID(str(e.version_id)),
                    )
                    found = resolve_citation(session, fresh, ref)
                    if found is not None:
                        resolved[e.evidence_id] = found
        usable = {n: eid for n, eid in bundle.citation_map.items() if eid in resolved}
        self._event("stage", {"stage": "verifying"})
        verification = verify(
            text,
            usable,
            bundle.used_evidence,
            config=VerifyConfig.from_settings(cs),
            embedder=self._meter.embedder(self.deps.embedder),
        )
        citations = [
            citation_payload(vc, resolved[vc.evidence_id])
            for vc in verification.citations
            if vc.evidence_id in resolved
        ]
        self._record_firewall()
        outcome = self._complete(verification, citations)
        self._capture(bundle, verification.answer_text, scope)
        self._remember_for_shadow(verification, scope)
        return outcome

    def _capture(self, bundle: PromptBundle, answer: str, scope: RetrievalScope) -> None:
        """Redacted prompt/response capture (M13.02): off unless the org enabled it; best effort,
        never fails or logs the run (text is never logged)."""
        try:
            from app.modules.observability.capture import record_capture

            workspace = next(iter(scope.workspace_ids)) if len(scope.workspace_ids) == 1 else None
            with self._session() as session:
                record_capture(
                    session,
                    org_id=self.claim.org_id,
                    run_id=self.claim.run_id,
                    workspace_id=workspace,
                    prompt_version_id=self._prompt.version_id,
                    messages=[(m["role"], m["content"]) for m in bundle.messages],
                    answer=answer,
                    chunk_ids=[uuid.UUID(str(e.chunk_id)) for e in bundle.used_evidence],
                    document_ids=[uuid.UUID(str(e.document_id)) for e in bundle.used_evidence],
                )
        except Exception as exc:  # noqa: BLE001 - capture must never break a finished run
            log.warning("chat run=%s capture failed (%s)", self.claim.run_id, type(exc).__name__)

    def _remember_for_shadow(self, verification: Verification, scope: RetrievalScope) -> None:
        if self._seed_parts is None:
            return
        question, history, evidence, latency_ms = self._seed_parts
        workspaces = scope.workspace_ids

        def still_valid() -> bool:
            fresh = self._resolve_scope(workspaces)
            if fresh is None:
                return False
            with self._session() as session:
                return all(
                    resolve_citation(
                        session,
                        fresh,
                        CitationRef(
                            chunk_id=uuid.UUID(str(e.chunk_id)),
                            document_id=uuid.UUID(str(e.document_id)),
                            version_id=uuid.UUID(str(e.version_id)),
                        ),
                    )
                    is not None
                    for e in evidence
                )

        self.shadow_seed = ShadowSeed(
            question=question,
            history=history,
            evidence=evidence,
            verification=verification,
            latency_ms=latency_ms,
            prompt_version_id=self._prompt.version_id,
            still_valid=still_valid,
            style=self._style,
        )

    def _complete(self, verification: Verification, citations: list[dict[str, Any]]) -> RunOutcome:
        """citations, verification, the assistant message (kept text only), completed, state: one
        fenced transaction, so a run never ends half-written."""
        vp = verification_payload(verification)
        with self._session() as session:
            self._check()
            chat_repo.append_event(
                session, self.fence, type="citations", data={"citations": citations}
            )
            chat_repo.append_event(session, self.fence, type="verification", data=vp)
            message_id = chat_repo.append_assistant_message(
                session,
                self.fence,
                thread_id=self.claim.thread_id,
                content=verification.answer_text,
                citations=citations,
                verification=vp,
            )
            chat_repo.append_event(
                session,
                self.fence,
                type="completed",
                data={"message_id": str(message_id), "answer_text": verification.answer_text},
            )
            chat_repo.finish_run(session, self.fence, state=STATE_COMPLETED)
        log.info(
            "chat run=%s completed status=%s citations=%d withheld=%d",
            self.claim.run_id,
            verification.status,
            len(citations),
            len(verification.withheld),
        )
        return "completed"
