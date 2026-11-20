"""User provisioning on first sign-in (M01.01.03; docs/architecture/m01-contracts.md §1, §2, §5).

:class:`DbUserProvisioner` implements :class:`app.core.security.UserProvisioner`. For every
authenticated request it upserts the caller's ``users`` row keyed on the verified external
identity (``issuer``, ``subject``) in one transaction, as the app role under RLS:

1. ``tenant_session(user_id=None, org_id=None)`` opens the transaction with no tenant context.
2. ``set_identity_context`` sets the verified (iss, sub) so the ``users`` policies admit exactly
   that row (DC-014).
3. ``INSERT ... ON CONFLICT (issuer, subject) DO UPDATE ... RETURNING id`` is atomic: concurrent
   first requests for the same identity serialize on the unique index, exactly one inserts and the
   others update the same row. The candidate id is generated here, so "the returned id is ours"
   means this call created the user.
4. Only on creation, ``app.current_user_id`` is set to the new id and a ``user.provisioned`` audit
   event (``org_id`` NULL, actor = the new user) is written in the same transaction, so a user row
   never exists without its event and a retry never writes a second one.

The audit row is written by :func:`_record_user_provisioned`, a minimal local insert. M01.03.04
(``app/modules/audit``) may replace it with ``record_event``; the row shape is the same.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, insert, text
from sqlalchemy.orm import Session

from app.core.db import set_identity_context, set_tenant_context, tenant_session
from app.core.models import AuditEvent
from app.core.security import Principal, VerifiedClaims

USER_PROVISIONED = "user.provisioned"

# email / display_name are refreshed from the latest token; a token that omits them (e.g. a
# narrower scope) keeps the stored value instead of erasing it.
_UPSERT_USER = text(
    """
    INSERT INTO users (id, issuer, subject, email, email_verified, display_name, created_at,
                       last_seen_at)
    VALUES (:id, :issuer, :subject, :email, :email_verified, :display_name, now(), now())
    ON CONFLICT (issuer, subject) DO UPDATE SET
        email_verified = CASE WHEN EXCLUDED.email IS NULL THEN users.email_verified
                              ELSE EXCLUDED.email_verified END,
        email = COALESCE(EXCLUDED.email, users.email),
        display_name = COALESCE(EXCLUDED.display_name, users.display_name),
        last_seen_at = now()
    RETURNING id, email, display_name
    """
)


_CONSUME_PREAPPROVALS = text("SELECT preapproval_consume(:uid)")


class DbUserProvisioner:
    """Idempotent, race-safe ``users`` upsert plus a one-time ``user.provisioned`` audit event."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def provision(self, claims: VerifiedClaims) -> Principal:
        candidate_id = uuid.uuid4()
        with tenant_session(user_id=None, org_id=None, engine=self._engine) as session:
            set_identity_context(session, issuer=claims.issuer, subject=claims.subject)
            row = session.execute(
                _UPSERT_USER,
                {
                    "id": candidate_id,
                    "issuer": claims.issuer,
                    "subject": claims.subject,
                    "email": claims.email,
                    "email_verified": claims.email_verified and claims.email is not None,
                    "display_name": claims.name,
                },
            ).one()
            user_id = uuid.UUID(str(row.id))
            set_tenant_context(session, user_id=user_id, org_id=None)
            if user_id == candidate_id:
                _record_user_provisioned(
                    session, user_id=user_id, email_verified=claims.email_verified
                )
            if claims.email_verified and claims.email is not None:
                # Pre-approved memberships (migration 0011); matches the VERIFIED email only.
                session.execute(_CONSUME_PREAPPROVALS, {"uid": user_id})
        return Principal(
            user_id=user_id,
            issuer=claims.issuer,
            subject=claims.subject,
            email=row.email,
            display_name=row.display_name,
        )


def _record_user_provisioned(session: Session, *, user_id: uuid.UUID, email_verified: bool) -> None:
    """Org-less audit insert without RETURNING (the SELECT policy hides org_id NULL rows).

    Metadata holds no token material and no PII beyond what the users row already stores.
    """
    session.execute(
        insert(AuditEvent).values(
            id=uuid.uuid4(),
            org_id=None,
            actor_user_id=user_id,
            event_type=USER_PROVISIONED,
            target_type="user",
            target_id=str(user_id),
            outcome="success",
            request_id=None,
            metadata_={"email_verified": email_verified},
        )
    )
