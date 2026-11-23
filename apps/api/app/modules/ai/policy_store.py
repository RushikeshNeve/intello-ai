"""DB-backed per-org AI policy (M04.02.01; contract m04 §5).

No row = the default: local models only, nothing external. A policy only *narrows or opens* what the
server already allows: an external provider is used only when ``AI_EXTERNAL_PROVIDERS_ENABLED`` is
true AND the org's row allows it (enforced in the gateway's ``get_chat_model``, the single choke
point). All functions take an RLS-scoped session (``tenant_session`` / worker session).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.ai.models.policy import ModelPolicy as ModelPolicy  # one canonical class
from app.modules.ai.models_db import AI_PROVIDERS, PROVIDER_LOCAL, AiPolicy

DEFAULT_PROVIDERS: Final = frozenset({PROVIDER_LOCAL})


class InvalidPolicy(ValueError):
    """The requested policy is not storable (unknown provider, empty list, external not allowed)."""


@dataclass(frozen=True, slots=True)
class StoredPolicy:
    policy: ModelPolicy
    updated_at: datetime | None  # None = default (no row)
    updated_by: uuid.UUID | None

    @property
    def is_default(self) -> bool:
        return self.updated_at is None


# ONE canonical ModelPolicy (the gateway's, ``ai.models.policy``): re-exported here for the store.
DEFAULT_POLICY: Final = ModelPolicy()


def validate_policy(
    allow_external: bool, providers: Iterable[str], hosted_models_allowed: bool = False
) -> ModelPolicy:
    """Raise :class:`InvalidPolicy` unless the combination can be stored (mirrors the CHECKs)."""
    chosen = frozenset(providers)
    if not chosen:
        raise InvalidPolicy("allowed_providers must not be empty")
    unknown = chosen - set(AI_PROVIDERS)
    if unknown:
        raise InvalidPolicy("unknown provider")
    if not allow_external and chosen != DEFAULT_PROVIDERS:
        raise InvalidPolicy("external providers require allow_external")
    return ModelPolicy(
        allow_external=allow_external,
        allowed_providers=chosen,
        hosted_models_allowed=hosted_models_allowed,
    )


def load_stored_policy(session: Session, org_id: uuid.UUID) -> StoredPolicy:
    row = session.execute(
        select(
            AiPolicy.allow_external,
            AiPolicy.allowed_providers,
            AiPolicy.updated_at,
            AiPolicy.pii_outbound,
            AiPolicy.pii_restore,
            AiPolicy.pii_secrets,
            AiPolicy.hosted_models_allowed,
        ).where(AiPolicy.org_id == org_id)
    ).one_or_none()
    if row is None:
        return StoredPolicy(DEFAULT_POLICY, None, None)
    updated_by = session.execute(
        select(AiPolicy.updated_by).where(AiPolicy.org_id == org_id)
    ).scalar_one()
    policy = ModelPolicy(
        bool(row.allow_external),
        frozenset(row.allowed_providers),
        hosted_models_allowed=bool(row.hosted_models_allowed),
        pii_outbound=row.pii_outbound,
        pii_restore=bool(row.pii_restore),
        pii_secrets=row.pii_secrets,
    )
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
    hosted_models_allowed: bool = False,
) -> StoredPolicy:
    """Upsert the org's policy (RLS lets only an org owner/admin do this)."""
    policy = validate_policy(allow_external, allowed_providers, hosted_models_allowed)
    providers = sorted(policy.allowed_providers)
    stmt = pg_insert(AiPolicy).values(
        org_id=org_id,
        allow_external=policy.allow_external,
        allowed_providers=providers,
        hosted_models_allowed=policy.hosted_models_allowed,
        updated_by=updated_by,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AiPolicy.org_id],
        set_={
            "allow_external": stmt.excluded.allow_external,
            "allowed_providers": stmt.excluded.allowed_providers,
            "hosted_models_allowed": stmt.excluded.hosted_models_allowed,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)
    return load_stored_policy(session, org_id)


def save_pii_policy(
    session: Session,
    org_id: uuid.UUID,
    *,
    pii_outbound: str,
    pii_restore: bool = False,
    pii_secrets: str = "block",
    updated_by: uuid.UUID,
) -> StoredPolicy:
    """M14.04: set the outbound PII guard on the org's policy row (created local-only if absent)."""
    if pii_outbound not in ("off", "redact", "block") or pii_secrets not in ("block", "redact"):
        raise InvalidPolicy("invalid pii policy")
    stmt = pg_insert(AiPolicy).values(
        org_id=org_id,
        pii_outbound=pii_outbound,
        pii_restore=pii_restore,
        pii_secrets=pii_secrets,
        updated_by=updated_by,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AiPolicy.org_id],
        set_={
            "pii_outbound": stmt.excluded.pii_outbound,
            "pii_restore": stmt.excluded.pii_restore,
            "pii_secrets": stmt.excluded.pii_secrets,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)
    return load_stored_policy(session, org_id)
