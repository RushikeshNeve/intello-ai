"""Memory / research side of the RLS world (M07 tables, migration 0010) and its grant registry.

Seeds, as the admin role, per tenant of :class:`rls_world.World`: a private ``suggested`` memory of
the tenant's plain member (``creator``), a workspace-visible ``approved`` and a workspace-visible
``suggested`` memory of the tenant's owner (workspace admin), and one research session of the
member with a step and a report. ``M07_GRANTS`` is the hand-written expectation of what each
least-privilege role may do on each table; ``test_19_m07_tables.py`` cross-checks the live catalog.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from rls_docs_world import Grant, _g
from rls_world import Tenant, World
from sqlalchemy import Engine, text

M07_TABLES: tuple[str, ...] = (
    "memories",
    "research_sessions",
    "research_steps",
    "research_reports",
)
ROLES: tuple[str, ...] = ("app", "worker")

M07_GRANTS: dict[str, dict[str, Grant]] = {
    "app": {
        "memories": _g(True, True, "state approved_by approved_at updated_at deleted_at"),
        "research_sessions": _g(True, True),
        "research_steps": _g(True),
        "research_reports": _g(True),
    },
    "worker": {
        "memories": _g(True),
        "research_sessions": _g(
            True,
            False,
            "state plan usage error_code started_at finished_at lease_owner lease_expires_at",
        ),
        "research_steps": _g(True, True, "state result started_at finished_at"),
        "research_reports": _g(True, True),
    },
}

CASES: list[tuple[str, str]] = [(r, t) for r in ROLES for t in M07_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]


@dataclass(frozen=True)
class M07Scope:
    tenant: Tenant
    memory: uuid.UUID  # private, suggested, owner = the member
    ws_approved: uuid.UUID  # workspace-visible, approved, owner = the tenant owner
    ws_suggested: uuid.UUID  # workspace-visible, suggested, owner = the tenant owner
    session: uuid.UUID  # created_by = the member
    step: uuid.UUID

    @property
    def org(self) -> uuid.UUID:
        return self.tenant.org_id

    @property
    def creator(self) -> uuid.UUID:
        return self.tenant.member_id

    def pk(self, table: str) -> uuid.UUID:
        key = {
            "memories": self.memory,
            "research_sessions": self.session,
            "research_steps": self.step,
            "research_reports": self.session,
        }[table]
        return key

    def pk_column(self, table: str) -> str:
        return "session_id" if table == "research_reports" else "id"


@dataclass(frozen=True)
class M07World:
    a: M07Scope
    b: M07Scope


_EVIDENCE = json.dumps(
    [
        {
            "chunk_id": str(uuid.uuid4()),
            "document_id": str(uuid.uuid4()),
            "version_id": str(uuid.uuid4()),
            "workspace_id": None,
        }
    ]
)


def _seed(conn: Any, t: Tenant) -> M07Scope:
    memory, ws_ok, ws_sug, session, step = (uuid.uuid4() for _ in range(5))
    insert = (
        "INSERT INTO memories (id, org_id, workspace_id, owner_id, visibility, kind, statement,"
        " state, evidence, approved_at) VALUES (:i, :o, :w, :u, :v, 'fact', :s, :st,"
        " CAST(:e AS jsonb), :ap)"
    )
    rows = (
        (memory, None, t.member_id, "private", "suggested", None),
        (ws_ok, t.workspace_id, t.owner_id, "workspace", "approved", "now"),
        (ws_sug, t.workspace_id, t.owner_id, "workspace", "suggested", None),
    )
    for ident, ws, user, vis, state, approved in rows:
        conn.execute(
            text(insert.replace(":ap", "now()" if approved else "NULL")),
            {
                "i": ident,
                "o": t.org_id,
                "w": ws,
                "u": user,
                "v": vis,
                "s": f"memory {t.marker}",
                "st": state,
                "e": _EVIDENCE,
            },
        )
    conn.execute(
        text(
            "INSERT INTO research_sessions (id, org_id, created_by, question)"
            " VALUES (:i, :o, :u, :q)"
        ),
        {"i": session, "o": t.org_id, "u": t.member_id, "q": f"question {t.marker}"},
    )
    conn.execute(
        text(
            "INSERT INTO research_steps (id, org_id, session_id, seq, kind)"
            " VALUES (:i, :o, :s, 0, 'plan')"
        ),
        {"i": step, "o": t.org_id, "s": session},
    )
    conn.execute(
        text("INSERT INTO research_reports (session_id, org_id) VALUES (:s, :o)"),
        {"s": session, "o": t.org_id},
    )
    return M07Scope(t, memory, ws_ok, ws_sug, session, step)


def seed_m07_world(engine: Engine, world: World) -> M07World:
    with engine.begin() as conn:
        return M07World(_seed(conn, world.a), _seed(conn, world.b))
