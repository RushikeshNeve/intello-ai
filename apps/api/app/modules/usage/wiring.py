"""Ready-made meters for the places that call models (run executors, ingestion, graph, search)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.modules.ai.embeddings.ports import EmbeddingProvider
from app.modules.ai.models.types import ChatModel
from app.modules.usage.limits import QuotaGuard
from app.modules.usage.meter import meter_chat_model, meter_embedder
from app.modules.usage.recorder import UsageContext, UsageRecorder


class Meter:
    """Binds a recorder and the who/what of one unit of work (a run, a job, a request)."""

    def __init__(
        self,
        sessions: Callable[[uuid.UUID], AbstractContextManager[Session]],
        *,
        org_id: uuid.UUID,
        chat_kind: str = "chat",
        user_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        enforce_chat: bool = False,
        enforce_embed_query: bool = False,
    ) -> None:
        # M11.08: enforce_* turn on quota checks before chat/research calls and query embeddings.
        self._guard = QuotaGuard(sessions, org_id=org_id, user_id=user_id)
        self._enforce_chat, self._enforce_embed = enforce_chat, enforce_embed_query
        self._recorder = UsageRecorder(sessions)
        self._org_id, self._user_id = org_id, user_id
        self._workspace_id, self._run_id, self._chat_kind = workspace_id, run_id, chat_kind

    def _ctx(self, kind: str) -> UsageContext:
        return UsageContext(self._org_id, kind, self._user_id, self._workspace_id, self._run_id)

    def chat(self, model: ChatModel) -> ChatModel:
        guard = self._guard if self._enforce_chat else None
        return meter_chat_model(model, self._recorder, self._ctx(self._chat_kind), guard)

    def embedder(self, embedder: EmbeddingProvider | None) -> EmbeddingProvider | None:
        guard = self._guard if self._enforce_embed else None
        return meter_embedder(embedder, self._recorder, self._ctx("embed"), guard)


def run_meter(
    engine: Engine,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
    kind: str,
) -> Meter:
    """Meter for a chat / research run executor (worker role, acting for the run's creator)."""
    from app.modules.ai import chat_repo

    def sessions(org: uuid.UUID) -> AbstractContextManager[Session]:
        return chat_repo.worker_chat_session(engine, org_id=org, user_id=user_id)

    return Meter(
        sessions,
        org_id=org_id,
        chat_kind=kind,
        user_id=user_id,
        run_id=run_id,
        enforce_chat=True,
    )
