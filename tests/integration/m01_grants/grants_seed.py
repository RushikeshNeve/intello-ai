"""Seeding for the grants suite (admin role, bypasses RLS)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from m01_seed import add_member, insert_user, seed_tenant
from sqlalchemy import Engine, text


@dataclass(frozen=True)
class GrantTenant:
    """An org with an owner, a workspace, a plain org member, an editor and a viewer."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
    owner_id: uuid.UUID
    member_id: uuid.UUID  # org member with no workspace membership
    editor_id: uuid.UUID
    viewer_id: uuid.UUID


def seed_grant_tenant(engine: Engine) -> GrantTenant:
    base = seed_tenant(engine)
    with engine.begin() as conn:
        member, editor, viewer = (insert_user(conn) for _ in range(3))
        for user in (member, editor, viewer):
            add_member(conn, base.org_id, user, "member")
        for user, role in ((editor, "editor"), (viewer, "viewer")):
            conn.execute(
                text(
                    "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
                    " VALUES (:w, :o, :u, :r)"
                ),
                {"w": base.workspace_id, "o": base.org_id, "u": user, "r": role},
            )
    return GrantTenant(
        org_id=base.org_id,
        workspace_id=base.workspace_id,
        owner_id=base.owner_id,
        member_id=member,
        editor_id=editor,
        viewer_id=viewer,
    )
