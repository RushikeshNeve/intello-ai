"""Role-matrix tests for the RBAC policy (M01.03.01).

Contract: docs/architecture/m01-contracts.md section 3. The expected tables below are written out by
hand from the contract, independently of the policy's own lookup tables, so a change to either side
shows up as a failing case.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.modules.authorization.policy import (
    Action,
    Decision,
    GrantPermission,
    OrgRole,
    SubjectContext,
    WorkspaceRole,
    can_assign_org_role,
    decide,
    is_concealed,
)

USER = uuid.UUID("00000000-0000-4000-8000-000000000001")

# Reason abbreviations used in the hand-written tables.
CODES = {
    "O": "allowed_by_org_role",
    "W": "allowed_by_workspace_role",
    "G": "allowed_by_grant",
    "N": "not_org_member",
    "X": "not_workspace_member",
    "I": "insufficient_role",
    "U": "unknown_action",
}
ALLOWING = {"O", "W", "G"}
CONCEALING = {"N", "X"}

ORG_ROWS: tuple[OrgRole | None, ...] = (OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MEMBER, None)
WS_COLS: tuple[WorkspaceRole | None, ...] = (
    WorkspaceRole.ADMIN,
    WorkspaceRole.EDITOR,
    WorkspaceRole.VIEWER,
    None,
)

# action -> 4 rows (org owner, admin, member, none) of 4 columns (ws admin, editor, viewer, none).
# fmt: off
EXPECTED_MATRIX: dict[str, tuple[str, str, str, str]] = {
    "org.read":                 ("OOOO", "OOOO", "OOOO", "NNNN"),
    "org.update":               ("OOOO", "OOOO", "IIII", "NNNN"),
    "org.delete":               ("OOOO", "IIII", "IIII", "NNNN"),
    "org.members.read":         ("OOOO", "OOOO", "OOOO", "NNNN"),
    "org.members.manage":       ("OOOO", "OOOO", "IIII", "NNNN"),
    "workspace.create":         ("OOOO", "OOOO", "IIII", "NNNN"),
    "workspace.read":           ("OOOO", "OOOO", "WWWX", "NNNN"),
    "workspace.update":         ("OOOO", "OOOO", "WIIX", "NNNN"),
    "workspace.delete":         ("OOOO", "OOOO", "WIIX", "NNNN"),
    "workspace.members.manage": ("OOOO", "OOOO", "WIIX", "NNNN"),
    "document.read":            ("OOOO", "OOOO", "WWWX", "NNNN"),
    "document.write":           ("OOOO", "OOOO", "WWIX", "NNNN"),
    "document.manage":          ("OOOO", "OOOO", "WIIX", "NNNN"),
}
# fmt: on


def _matrix_cases() -> list[tuple[str, OrgRole | None, WorkspaceRole | None, str]]:
    cases = []
    for action, rows in EXPECTED_MATRIX.items():
        for org_role, row in zip(ORG_ROWS, rows, strict=True):
            for ws_role, code in zip(WS_COLS, row, strict=True):
                cases.append((action, org_role, ws_role, code))
    return cases


MATRIX_CASES = _matrix_cases()


def _subject(
    org_role: OrgRole | None,
    ws_role: WorkspaceRole | None = None,
    grants: frozenset[GrantPermission] = frozenset(),
) -> SubjectContext:
    return SubjectContext(user_id=USER, org_role=org_role, workspace_role=ws_role, grants=grants)


def _check(decision: Decision, code: str) -> None:
    assert decision == Decision(allowed=code in ALLOWING, reason=CODES[code])
    assert is_concealed(decision) is (code in CONCEALING)


def test_action_values_are_exactly_the_contract() -> None:
    assert {a.value for a in Action} == set(EXPECTED_MATRIX)
    assert len(MATRIX_CASES) == 13 * 4 * 4


@pytest.mark.parametrize(("action", "org_role", "ws_role", "code"), MATRIX_CASES)
def test_role_matrix(
    action: str, org_role: OrgRole | None, ws_role: WorkspaceRole | None, code: str
) -> None:
    _check(decide(_subject(org_role, ws_role), Action(action)), code)


@pytest.mark.parametrize(("action", "org_role", "ws_role", "code"), MATRIX_CASES)
def test_role_matrix_accepts_plain_strings(
    action: str, org_role: OrgRole | None, ws_role: WorkspaceRole | None, code: str
) -> None:
    _check(decide(_subject(org_role, ws_role), action), code)


R, W, M = GrantPermission.READ, GrantPermission.WRITE, GrantPermission.MANAGE

# Plain org member. Key: (workspace role, grants) -> codes for (document.read, write, manage).
EXPECTED_GRANTS: dict[tuple[WorkspaceRole | None, frozenset[GrantPermission]], str] = {
    (None, frozenset()): "XXX",
    (None, frozenset({R})): "GII",
    (None, frozenset({W})): "GGI",
    (None, frozenset({M})): "GGG",
    (None, frozenset({R, W})): "GGI",
    (None, frozenset({R, M})): "GGG",
    (None, frozenset({R, W, M})): "GGG",
    (WorkspaceRole.VIEWER, frozenset()): "WII",
    (WorkspaceRole.VIEWER, frozenset({R})): "WII",
    (WorkspaceRole.VIEWER, frozenset({W})): "WGI",
    (WorkspaceRole.VIEWER, frozenset({M})): "WGG",
    (WorkspaceRole.EDITOR, frozenset({R})): "WWI",
    (WorkspaceRole.EDITOR, frozenset({M})): "WWG",
    (WorkspaceRole.ADMIN, frozenset({M})): "WWW",
}


@pytest.mark.parametrize(
    ("ws_role", "grants", "action", "code"),
    [
        (ws_role, grants, action, code)
        for (ws_role, grants), codes in EXPECTED_GRANTS.items()
        for action, code in zip(
            (Action.DOCUMENT_READ, Action.DOCUMENT_WRITE, Action.DOCUMENT_MANAGE),
            codes,
            strict=True,
        )
    ],
)
def test_document_grants_for_org_member(
    ws_role: WorkspaceRole | None, grants: frozenset[GrantPermission], action: Action, code: str
) -> None:
    _check(decide(_subject(OrgRole.MEMBER, ws_role, grants), action), code)


@pytest.mark.parametrize(
    ("ws_role", "action", "code"),
    [
        (None, Action.WORKSPACE_READ, "X"),
        (None, Action.WORKSPACE_UPDATE, "X"),
        (None, Action.WORKSPACE_MEMBERS_MANAGE, "X"),
        (WorkspaceRole.VIEWER, Action.WORKSPACE_UPDATE, "I"),
        (WorkspaceRole.VIEWER, Action.WORKSPACE_DELETE, "I"),
        (None, Action.ORG_UPDATE, "I"),
        (None, Action.WORKSPACE_CREATE, "I"),
    ],
)
def test_grants_never_authorize_org_or_workspace_actions(
    ws_role: WorkspaceRole | None, action: Action, code: str
) -> None:
    _check(decide(_subject(OrgRole.MEMBER, ws_role, frozenset({R, W, M})), action), code)


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("ws_role", WS_COLS)
def test_non_member_with_grants_is_denied_and_concealed(
    action: Action, ws_role: WorkspaceRole | None
) -> None:
    decision = decide(_subject(None, ws_role, frozenset({R, W, M})), action)
    _check(decision, "N")


@pytest.mark.parametrize(
    "action",
    [
        "",
        "org",
        "ORG.READ",
        "Org.Read",
        " org.read",
        "org.read ",
        "document.*",
        "*",
        "document.delete",
        "workspace.members.read",
        "org.owner",
        "admin",
        "org.read\x00",
    ],
)
@pytest.mark.parametrize("org_role", [OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MEMBER])
def test_unknown_action_is_denied(action: str, org_role: OrgRole) -> None:
    decision = decide(_subject(org_role, WorkspaceRole.ADMIN, frozenset({R, W, M})), action)
    _check(decision, "U")


def test_unknown_action_for_non_member_still_conceals() -> None:
    _check(decide(_subject(None), "no.such.action"), "N")


def test_org_role_given_as_plain_string_is_honoured() -> None:
    subject = SubjectContext(user_id=USER, org_role="owner", workspace_role=None)  # type: ignore[arg-type]
    _check(decide(subject, Action.ORG_DELETE), "O")
    _check(can_assign_org_role(subject, OrgRole.MEMBER, OrgRole.OWNER), "O")


def test_unrecognized_org_role_is_treated_as_non_member() -> None:
    subject = SubjectContext(user_id=USER, org_role="superuser", workspace_role=None)  # type: ignore[arg-type]
    _check(decide(subject, Action.ORG_READ), "N")
    _check(can_assign_org_role(subject, OrgRole.MEMBER, OrgRole.ADMIN), "N")


TARGETS: tuple[OrgRole | None, ...] = (OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MEMBER, None)

# actor -> 4 rows (target current: owner, admin, member, none=new member)
#       of 4 columns (target new: owner, admin, member, none=removal).
# fmt: off
EXPECTED_ASSIGN: dict[OrgRole | None, tuple[str, str, str, str]] = {
    OrgRole.OWNER:  ("OOOO", "OOOO", "OOOO", "OOOU"),
    OrgRole.ADMIN:  ("IIII", "IOOO", "IOOO", "IOOU"),
    OrgRole.MEMBER: ("IIII", "IIII", "IIII", "IIIU"),
    None:           ("NNNN", "NNNN", "NNNN", "NNNN"),
}
# fmt: on


@pytest.mark.parametrize(
    ("actor_role", "current", "new", "code"),
    [
        (actor_role, current, new, code)
        for actor_role, rows in EXPECTED_ASSIGN.items()
        for current, row in zip(TARGETS, rows, strict=True)
        for new, code in zip(TARGETS, row, strict=True)
    ],
)
def test_can_assign_org_role_matrix(
    actor_role: OrgRole | None, current: OrgRole | None, new: OrgRole | None, code: str
) -> None:
    _check(can_assign_org_role(_subject(actor_role), current, new), code)


def test_can_assign_org_role_ignores_workspace_role_and_grants() -> None:
    actor = _subject(OrgRole.MEMBER, WorkspaceRole.ADMIN, frozenset({R, W, M}))
    _check(can_assign_org_role(actor, None, OrgRole.MEMBER), "I")


def test_can_assign_org_role_rejects_unknown_target_role() -> None:
    actor = _subject(OrgRole.OWNER)
    _check(can_assign_org_role(actor, OrgRole.MEMBER, "root"), "U")  # type: ignore[arg-type]
    _check(can_assign_org_role(actor, "root", OrgRole.MEMBER), "U")  # type: ignore[arg-type]


def test_subject_context_is_immutable_and_hashable() -> None:
    subject = _subject(OrgRole.MEMBER, WorkspaceRole.VIEWER, frozenset({R}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        subject.org_role = OrgRole.OWNER  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        subject.grants = frozenset({M})  # type: ignore[misc]
    assert hash(subject) == hash(_subject(OrgRole.MEMBER, WorkspaceRole.VIEWER, frozenset({R})))


def test_subject_context_defaults_to_no_grants() -> None:
    subject = SubjectContext(user_id=USER, org_role=OrgRole.MEMBER, workspace_role=None)
    assert subject.grants == frozenset()
    assert isinstance(subject.grants, frozenset)


def test_decision_is_immutable() -> None:
    decision = decide(_subject(OrgRole.MEMBER), Action.ORG_READ)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.reason = "x"  # type: ignore[misc]


def test_decide_is_deterministic_and_does_not_mutate_subject() -> None:
    subject = _subject(OrgRole.MEMBER, WorkspaceRole.EDITOR, frozenset({R}))
    before = dataclasses.replace(subject)
    first = [decide(subject, a) for a in Action]
    assert [decide(subject, a) for a in Action] == first
    assert subject == before


def test_is_concealed_only_for_non_membership_denials() -> None:
    assert is_concealed(Decision(allowed=False, reason="not_org_member"))
    assert is_concealed(Decision(allowed=False, reason="not_workspace_member"))
    for reason in ("insufficient_role", "unknown_action", "something_else"):
        assert not is_concealed(Decision(allowed=False, reason=reason))
    for reason in ("allowed_by_org_role", "not_org_member"):
        assert not is_concealed(Decision(allowed=True, reason=reason))


def test_enum_values() -> None:
    assert [r.value for r in OrgRole] == ["owner", "admin", "member"]
    assert [r.value for r in WorkspaceRole] == ["admin", "editor", "viewer"]
    assert [p.value for p in GrantPermission] == ["read", "write", "manage"]
    assert list(EXPECTED_MATRIX) == [a.value for a in Action]
