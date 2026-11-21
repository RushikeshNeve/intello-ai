"""Authorized retrieval scope (M04.01.01, contract m04 §2).

The scope is derived on the server, per request, from PostgreSQL inside the caller's RLS-scoped
session. The client may only *narrow* it. :class:`RetrievalScope` can only be built by
:func:`resolve_scope` (a guard in ``__post_init__`` rejects direct construction).

Document-level grants do NOT widen the scope (DC-025): they allow opening one document only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.knowledge.ports import (
    MAX_WORKSPACES_PER_FILTER,
    OrgScope,
    SearchFilters,
    UnknownOrganization,
    validated_org_scope,
)

_MINT: Final = object()
_ORG_MANAGERS: Final = ("owner", "admin")


class EmptyScope(Exception):
    """The caller has no readable workspace (answer an empty result, never an error)."""


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """The workspaces ``user_id`` may read in ``org_id``. Obtain it with :func:`resolve_scope`."""

    org_id: uuid.UUID
    user_id: uuid.UUID
    workspace_ids: frozenset[uuid.UUID]
    org: OrgScope
    _mint: object = field(default=None, repr=False, compare=False)
    # The org's active embedding space (M12.05 pointer); None = the configured default space.
    active_space: str | None = None

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError("RetrievalScope can only be created with resolve_scope()")
        if not self.workspace_ids or len(self.workspace_ids) > MAX_WORKSPACES_PER_FILTER:
            raise ValueError("workspace_ids must hold 1..1000 ids")
        if self.org.org_id != self.org_id:
            raise ValueError("scope organization mismatch")

    def filters(self) -> SearchFilters:
        return SearchFilters(self.workspace_ids)


def _active_space(session: Session, org_id: uuid.UUID) -> str | None:
    found = session.execute(
        text("SELECT space_id FROM org_active_spaces WHERE org_id = :o"), {"o": org_id}
    ).scalar_one_or_none()
    return str(found) if found else None


def resolve_scope(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    requested_workspace_ids: Iterable[uuid.UUID] | None = None,
) -> RetrievalScope:
    """Derive the scope. Raises ``UnknownOrganization`` (non-member or no org: concealed 404)
    or :class:`EmptyScope`. The session must be an app-role tenant session of ``user_id``
    (it reads membership roles, which the worker role cannot)."""
    org = validated_org_scope(session, org_id)
    role = session.execute(
        text("SELECT role FROM organization_memberships WHERE org_id = :o AND user_id = :u"),
        {"o": org_id, "u": user_id},
    ).scalar_one_or_none()
    if role is None:
        raise UnknownOrganization("not a member")
    if role in _ORG_MANAGERS:
        rows = session.execute(
            text(
                "SELECT id FROM workspaces WHERE org_id = :o AND deleted_at IS NULL"
                " ORDER BY created_at, id LIMIT :n"
            ),
            {"o": org_id, "n": MAX_WORKSPACES_PER_FILTER},
        ).all()
    else:
        rows = session.execute(
            text(
                "SELECT w.id FROM workspaces w JOIN workspace_memberships m"
                " ON m.workspace_id = w.id AND m.org_id = w.org_id"
                " WHERE w.org_id = :o AND w.deleted_at IS NULL AND m.user_id = :u"
                " AND m.role IN ('viewer', 'editor', 'admin')"
                " ORDER BY w.created_at, w.id LIMIT :n"
            ),
            {"o": org_id, "u": user_id, "n": MAX_WORKSPACES_PER_FILTER},
        ).all()
    allowed = {r[0] for r in rows}
    if requested_workspace_ids is not None:
        allowed &= set(requested_workspace_ids)
    if not allowed:
        raise EmptyScope
    return RetrievalScope(
        org_id,
        user_id,
        frozenset(allowed),
        org,
        _mint=_MINT,
        active_space=_active_space(session, org_id),
    )


def scope_for_workspaces(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_ids: Iterable[uuid.UUID],
) -> RetrievalScope:
    """Mint a scope from workspace ids the caller has ALREADY derived server-side.

    For the chat worker only: it cannot read membership roles, so it re-derives the user's readable
    workspaces through the ``chat_readable_workspaces`` definer function, intersects them with the
    run's creation snapshot and passes the result here. The org is still proven live through
    ``session``. Never call this with ids that came from a request. Raises ``UnknownOrganization``
    or :class:`EmptyScope`.
    """
    org = validated_org_scope(session, org_id)
    allowed = frozenset(workspace_ids)
    if not allowed:
        raise EmptyScope
    return RetrievalScope(
        org_id, user_id, allowed, org, _mint=_MINT, active_space=_active_space(session, org_id)
    )
