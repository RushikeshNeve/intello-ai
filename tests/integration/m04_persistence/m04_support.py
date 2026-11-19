"""Helpers for the M04 persistence tests: a seeded chat world and small SQL utilities."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from m01_seed import add_member, insert_org, insert_user
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError


@dataclass(frozen=True)
class ChatWorld:
    """Org A (owner, admin, member, member2, a workspace) and org B (its own owner)."""

    org: uuid.UUID
    owner: uuid.UUID
    admin: uuid.UUID
    member: uuid.UUID
    member2: uuid.UUID
    ws: uuid.UUID
    org_b: uuid.UUID
    owner_b: uuid.UUID
    ws_b: uuid.UUID


def seed_world(engine: Engine) -> ChatWorld:
    with engine.begin() as conn:
        owner, admin, member, member2, owner_b = (insert_user(conn) for _ in range(5))
        org = insert_org(conn, created_by=owner)
        add_member(conn, org, owner, "owner")
        add_member(conn, org, admin, "admin")
        add_member(conn, org, member, "member")
        add_member(conn, org, member2, "member")
        org_b = insert_org(conn, created_by=owner_b)
        add_member(conn, org_b, owner_b, "owner")
        ws, ws_b = uuid.uuid4(), uuid.uuid4()
        for w, o, u in ((ws, org, owner), (ws_b, org_b, owner_b)):
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
                    " VALUES (:w, :o, :s, 'WS', :u)"
                ),
                {"w": w, "o": o, "s": f"ws-{uuid.uuid4().hex[:10]}", "u": u},
            )
    return ChatWorld(org, owner, admin, member, member2, ws, org_b, owner_b, ws_b)


def sqlstate(exc: BaseException) -> str | None:
    if isinstance(exc, DBAPIError):
        return getattr(exc.orig, "sqlstate", None)
    return None


def constraint(exc: BaseException) -> str | None:
    if isinstance(exc, DBAPIError):
        return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    return None


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]
