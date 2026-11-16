"""RBAC policy decision service (M01.03.01).

Pure and free of I/O: the caller loads a :class:`SubjectContext` (org role, workspace role and the
active grants on the target resource) and asks :func:`decide` whether an :class:`Action` is allowed.
The contract is ``docs/architecture/m01-contracts.md`` section 3.

Everything is denied unless a rule below explicitly allows it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class OrgRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class WorkspaceRole(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class GrantPermission(StrEnum):
    """Resource grant permission. Implication: ``manage`` implies ``write`` implies ``read``."""

    READ = "read"
    WRITE = "write"
    MANAGE = "manage"


class Action(StrEnum):
    ORG_READ = "org.read"
    ORG_UPDATE = "org.update"
    ORG_DELETE = "org.delete"
    ORG_MEMBERS_READ = "org.members.read"
    ORG_MEMBERS_MANAGE = "org.members.manage"
    WORKSPACE_CREATE = "workspace.create"
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_UPDATE = "workspace.update"
    WORKSPACE_DELETE = "workspace.delete"
    WORKSPACE_MEMBERS_MANAGE = "workspace.members.manage"
    DOCUMENT_READ = "document.read"
    DOCUMENT_WRITE = "document.write"
    DOCUMENT_MANAGE = "document.manage"


class Reason(StrEnum):
    """Stable machine-readable decision reasons."""

    ALLOWED_BY_ORG_ROLE = "allowed_by_org_role"
    ALLOWED_BY_WORKSPACE_ROLE = "allowed_by_workspace_role"
    ALLOWED_BY_GRANT = "allowed_by_grant"
    NOT_ORG_MEMBER = "not_org_member"
    NOT_WORKSPACE_MEMBER = "not_workspace_member"
    INSUFFICIENT_ROLE = "insufficient_role"
    UNKNOWN_ACTION = "unknown_action"


@dataclass(frozen=True, slots=True)
class SubjectContext:
    """Who is acting, relative to the target org/workspace/resource. Loaded by the caller."""

    user_id: uuid.UUID
    org_role: OrgRole | None  # None = not a member of the org
    workspace_role: WorkspaceRole | None  # None = no explicit workspace membership
    grants: frozenset[GrantPermission] = frozenset()  # active grants on the target resource


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str  # one of the Reason values


_ALL_ORG_ROLES: Final = frozenset(OrgRole)
_ORG_MANAGERS: Final = frozenset({OrgRole.OWNER, OrgRole.ADMIN})
_NO_WORKSPACE_ROLES: Final[frozenset[WorkspaceRole]] = frozenset()
_ALL_WORKSPACE_ROLES: Final = frozenset(WorkspaceRole)

# Org-scoped actions: decided by the org role alone.
_ORG_ACTIONS: Final[Mapping[Action, frozenset[OrgRole]]] = {
    Action.ORG_READ: _ALL_ORG_ROLES,
    Action.ORG_UPDATE: _ORG_MANAGERS,
    Action.ORG_DELETE: frozenset({OrgRole.OWNER}),
    Action.ORG_MEMBERS_READ: _ALL_ORG_ROLES,
    Action.ORG_MEMBERS_MANAGE: _ORG_MANAGERS,
    Action.WORKSPACE_CREATE: _ORG_MANAGERS,
}

# Workspace- and resource-scoped actions: org owner/admin are implicit workspace admins; otherwise
# the explicit workspace role decides, and for documents active grants may also allow.
_WORKSPACE_ACTIONS: Final[Mapping[Action, frozenset[WorkspaceRole]]] = {
    Action.WORKSPACE_READ: _ALL_WORKSPACE_ROLES,
    Action.WORKSPACE_UPDATE: frozenset({WorkspaceRole.ADMIN}),
    Action.WORKSPACE_DELETE: frozenset({WorkspaceRole.ADMIN}),
    Action.WORKSPACE_MEMBERS_MANAGE: frozenset({WorkspaceRole.ADMIN}),
    Action.DOCUMENT_READ: _ALL_WORKSPACE_ROLES,
    Action.DOCUMENT_WRITE: frozenset({WorkspaceRole.ADMIN, WorkspaceRole.EDITOR}),
    Action.DOCUMENT_MANAGE: frozenset({WorkspaceRole.ADMIN}),
}

# Which grant permissions satisfy each document action (manage > write > read).
_GRANT_ACTIONS: Final[Mapping[Action, frozenset[GrantPermission]]] = {
    Action.DOCUMENT_READ: frozenset(GrantPermission),
    Action.DOCUMENT_WRITE: frozenset({GrantPermission.WRITE, GrantPermission.MANAGE}),
    Action.DOCUMENT_MANAGE: frozenset({GrantPermission.MANAGE}),
}

_CONCEALED_REASONS: Final = frozenset({Reason.NOT_ORG_MEMBER, Reason.NOT_WORKSPACE_MEMBER})


def _allow(reason: Reason) -> Decision:
    return Decision(allowed=True, reason=reason.value)


def _deny(reason: Reason) -> Decision:
    return Decision(allowed=False, reason=reason.value)


def _parse_action(action: Action | str) -> Action | None:
    try:
        return Action(action)
    except ValueError:
        return None


def decide(subject: SubjectContext, action: Action | str) -> Decision:
    """Decide whether ``subject`` may perform ``action``. Deny by default.

    ``action`` may be an untrusted string; anything that is not an :class:`Action` value is denied
    with ``unknown_action``. A subject that is not an org member is denied everything with
    ``not_org_member``, whatever its grants, so routes can answer 404 without revealing that the org
    exists. An org member with no workspace role and no relevant grant is denied workspace and
    document actions with ``not_workspace_member`` (also 404).
    """
    if subject.org_role not in _ALL_ORG_ROLES:
        return _deny(Reason.NOT_ORG_MEMBER)

    parsed = _parse_action(action)
    if parsed is None:
        return _deny(Reason.UNKNOWN_ACTION)

    org_allowed = _ORG_ACTIONS.get(parsed)
    if org_allowed is not None:
        if subject.org_role in org_allowed:
            return _allow(Reason.ALLOWED_BY_ORG_ROLE)
        return _deny(Reason.INSUFFICIENT_ROLE)

    workspace_allowed = _WORKSPACE_ACTIONS.get(parsed, _NO_WORKSPACE_ROLES)
    if subject.org_role in _ORG_MANAGERS:
        return _allow(Reason.ALLOWED_BY_ORG_ROLE)
    if subject.workspace_role in workspace_allowed:
        return _allow(Reason.ALLOWED_BY_WORKSPACE_ROLE)

    grant_allowed = _GRANT_ACTIONS.get(parsed, frozenset())
    if subject.grants & grant_allowed:
        return _allow(Reason.ALLOWED_BY_GRANT)

    has_relevant_grant = bool(grant_allowed and subject.grants)
    if subject.workspace_role not in _ALL_WORKSPACE_ROLES and not has_relevant_grant:
        # Plain org member with no workspace membership and no grant on this resource: 404.
        return _deny(Reason.NOT_WORKSPACE_MEMBER)
    return _deny(Reason.INSUFFICIENT_ROLE)


def is_concealed(decision: Decision) -> bool:
    """True when a denial must be reported as HTTP 404 (non-membership), not 403."""
    return not decision.allowed and decision.reason in _CONCEALED_REASONS


def can_assign_org_role(
    actor: SubjectContext,
    target_current: OrgRole | None,
    target_new: OrgRole | None,
) -> Decision:
    """Decide whether ``actor`` may move a user from ``target_current`` to ``target_new`` org role.

    ``target_current=None`` means adding a new member; ``target_new=None`` means removing one.
    Owners may make any change; admins may only add, remove or change ``member``/``admin`` (never
    grant or revoke ``owner``); members and non-members may make none. A request with both None is
    not an action and is denied. Invariants that need I/O, such as keeping at least one owner, are
    the caller's job.
    """
    if actor.org_role not in _ALL_ORG_ROLES:
        return _deny(Reason.NOT_ORG_MEMBER)
    if target_current is None and target_new is None:
        return _deny(Reason.UNKNOWN_ACTION)
    for role in (target_current, target_new):
        if role is not None and role not in _ALL_ORG_ROLES:
            return _deny(Reason.UNKNOWN_ACTION)
    if actor.org_role == OrgRole.OWNER:
        return _allow(Reason.ALLOWED_BY_ORG_ROLE)
    if actor.org_role == OrgRole.ADMIN and OrgRole.OWNER not in (target_current, target_new):
        return _allow(Reason.ALLOWED_BY_ORG_ROLE)
    return _deny(Reason.INSUFFICIENT_ROLE)
