"""DB-backed per-org AI policy (M04.02.01; contract m04 §5).

No row = the default: local models only, nothing external. A policy only *narrows or opens* what the
server already allows: an external provider is used only when ``AI_EXTERNAL_PROVIDERS_ENABLED`` is
true AND the org's row allows it (enforced in the gateway's ``get_chat_model``, the single choke
point). All functions take an RLS-scoped session (``tenant_session`` / worker session).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.ai.models_db import AI_PROVIDERS, PROVIDER_LOCAL, AiPolicy

DEFAULT_PROVIDERS: Final = frozenset({PROVIDER_LOCAL})


class InvalidPolicy(ValueError):
    """The requested policy is not storable (unknown provider, empty list, external not allowed)."""


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    allow_external: bool = False
    allowed_providers: frozenset[str] = field(default=DEFAULT_PROVIDERS)


@dataclass(frozen=True, slots=True)
class StoredPolicy:
    policy: ModelPolicy
    updated_at: datetime | None  # None = default (no row)
    updated_by: uuid.UUID | None

    @property
    def is_default(self) -> bool:
        return self.updated_at is None


DEFAULT_POLICY: Final = ModelPolicy()


def validate_policy(allow_external: bool, providers: Iterable[str]) -> ModelPolicy:
    """Raise :class:`InvalidPolicy` unless the combination can be stored (mirrors the CHECKs)."""
    chosen = frozenset(providers)
    if not chosen:
        raise InvalidPolicy("allowed_providers must not be empty")
    unknown = chosen - set(AI_PROVIDERS)
    if unknown:
        raise InvalidPolicy("unknown provider")
    if not allow_external and chosen != DEFAULT_PROVIDERS:
        raise InvalidPolicy("external providers require allow_external")
    return ModelPolicy(allow_external=allow_external, allowed_providers=chosen)


def load_stored_policy(session: Session, org_id: uuid.UUID) -> StoredPolicy:
    row = session.execute(
        select(AiPolicy.allow_external, AiPolicy.allowed_providers, AiPolicy.updated_at).where(
            AiPolicy.org_id == org_id
        )
    ).one_or_none()
    if row is None:
        return StoredPolicy(DEFAULT_POLICY, None, None)
    updated_by = session.execute(
        select(AiPolicy.updated_by).where(AiPolicy.org_id == org_id)
    ).scalar_one()
    policy = ModelPolicy(bool(row.allow_external), frozenset(row.allowed_providers))
    return StoredPolicy(policy, row.updated_at, updated_by)


def load_policy(session: Session, org_id: uuid.UUID) -> ModelPolicy:
    """The org's policy; the default (local only) when no row exists."""
    return load_stored_policy(session, org_id).policy


def save_policy(
    session: Session,
    org_id: uuid.UUID,
    *,
    allow_external: bool,
    allowed_providers: Iterable[str],
    updated_by: uuid.UUID,
) -> StoredPolicy:
    """Upsert the org's policy (RLS lets only an org owner/admin do this)."""
    policy = validate_policy(allow_external, allowed_providers)
    providers = sorted(policy.allowed_providers)
    stmt = pg_insert(AiPolicy).values(
        org_id=org_id,
        allow_external=policy.allow_external,
        allowed_providers=providers,
        updated_by=updated_by,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AiPolicy.org_id],
        set_={
            "allow_external": stmt.excluded.allow_external,
            "allowed_providers": stmt.excluded.allowed_providers,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)
    return load_stored_policy(session, org_id)
