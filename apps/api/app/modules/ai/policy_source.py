"""DB-backed ``PolicySource`` for the gateway (M04.02.01; contract m04 §5).

The gateway's :class:`~app.modules.ai.models.policy.PolicySource` protocol asks for an org's
policy; this implementation reads it from ``ai_policies`` through an RLS-scoped session (API
tenant session or worker session). A read failure propagates: ``get_chat_model`` turns it into a
fail-closed ``PolicyDenied("policy_unavailable")``.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.modules.ai import policy_store
from app.modules.ai.models.policy import ModelPolicy


class DbPolicySource:
    """``get_policy(org_id)`` over one open session (no row = the local-only default)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_policy(self, org_id: uuid.UUID) -> ModelPolicy:
        return policy_store.load_policy(self._session, org_id)
