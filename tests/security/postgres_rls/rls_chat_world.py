"""Chat / AI-policy side of the RLS world (M04 tables, migration 0007) and its grant registry.

Seeds, as the admin role, per tenant of :class:`rls_world.World`: one private thread of the
tenant's plain member (``creator``), its user message, a queued run, one run event and an
``ai_policies`` row. The tenant's owner (``other``) is an org member who is NOT the creator and
must therefore see none of it. ``CHAT_GRANTS`` is the hand-written expectation of what each
least-privilege role may do on each table; ``test_17_chat_tables.py`` cross-checks it against the
live catalog.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from rls_docs_world import Grant, _g
from rls_world import Tenant, World
from sqlalchemy import Engine, text

CHAT_TABLES: tuple[str, ...] = (
    "ai_policies",
    "chat_threads",
    "chat_messages",
    "chat_runs",
    "chat_run_events",
)
# Tables whose rows are private to the thread/run creator (need org AND user context to be seen).
PRIVATE_TABLES: tuple[str, ...] = tuple(t for t in CHAT_TABLES if t != "ai_policies")
ROLES: tuple[str, ...] = ("app", "worker")

CHAT_GRANTS: dict[str, dict[str, Grant]] = {
    "app": {
        "ai_policies": _g(True, True, "allow_external allowed_providers updated_by updated_at"),
        "chat_threads": _g(True, True, "title deleted_at updated_at"),
        "chat_messages": _g(True, True),
        "chat_runs": _g(True, True),
        "chat_run_events": _g(True),
    },
    "worker": {
        "ai_policies": _g(True),
        "chat_threads": _g(True, False, "updated_at"),
        "chat_messages": _g(True, True),
        "chat_runs": _g(
            True,
            False,
            "state model started_at finished_at error_code lease_owner lease_expires_at",
        ),
        "chat_run_events": _g(True, True),
    },
}

CASES: list[tuple[str, str]] = [(r, t) for r in ROLES for t in CHAT_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]
SELECT_CASES = [(r, t) for r, t in CASES if CHAT_GRANTS[r][t].select]
SELECT_IDS = [f"{r}-{t}" for r, t in SELECT_CASES]
PRIVATE_SELECT_CASES = [(r, t) for r, t in SELECT_CASES if t in PRIVATE_TABLES]
PRIVATE_SELECT_IDS = [f"{r}-{t}" for r, t in PRIVATE_SELECT_CASES]


@dataclass(frozen=True)
class ChatScope:
    tenant: Tenant
    thread: uuid.UUID
    run: uuid.UUID  # queued
    message: uuid.UUID
    event_seq: int

    @property
    def org(self) -> uuid.UUID:
        return self.tenant.org_id

    @property
    def creator(self) -> uuid.UUID:
        return self.tenant.member_id

    @property
    def other(self) -> uuid.UUID:
        """An org member (the owner) who did not create the thread."""
        return self.tenant.owner_id

    @property
    def ws(self) -> uuid.UUID:
        return self.tenant.workspace_id


@dataclass(frozen=True)
class ChatWorld:
    a: ChatScope
    b: ChatScope


def _seed(conn: Any, t: Tenant) -> ChatScope:
    thread, run, message = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO chat_threads (id, org_id, workspace_id, created_by, title)"
            " VALUES (:t, :o, :w, :u, :title)"
        ),
        {
            "t": thread,
            "o": t.org_id,
            "w": t.workspace_id,
            "u": t.member_id,
            "title": f"chat-{t.marker}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO chat_runs (id, org_id, thread_id, user_message_id, created_by,"
            " idempotency_key) VALUES (:r, :o, :t, :m, :u, :k)"
        ),
        {
            "r": run,
            "o": t.org_id,
            "t": thread,
            "m": message,
            "u": t.member_id,
            "k": f"key-{run.hex[:8]}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO chat_messages (id, org_id, thread_id, role, content, run_id)"
            " VALUES (:m, :o, :t, 'user', :c, :r)"
        ),
        {"m": message, "o": t.org_id, "t": thread, "c": f"question-{t.marker}", "r": run},
    )
    seq = conn.execute(
        text(
            "INSERT INTO chat_run_events (run_id, org_id, type, data)"
            " VALUES (:r, :o, 'retrieval', CAST(:d AS jsonb)) RETURNING seq"
        ),
        {"r": run, "o": t.org_id, "d": '{"candidates": 3}'},
    ).scalar_one()
    conn.execute(
        text("INSERT INTO ai_policies (org_id, updated_by) VALUES (:o, :u)"),
        {"o": t.org_id, "u": t.owner_id},
    )
    return ChatScope(t, thread, run, message, int(seq))


def seed_chat_world(engine: Engine, world: World) -> ChatWorld:
    with engine.begin() as conn:
        a = _seed(conn, world.a)
        b = _seed(conn, world.b)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in CHAT_TABLES:
            conn.execute(text(f"ANALYZE {table}"))
    return ChatWorld(a, b)


def seed_extra_thread(engine: Engine, scope: ChatScope, *, deleted: bool = False) -> uuid.UUID:
    """One more thread of the scope's creator (admin; committed), optionally soft-deleted."""
    tid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chat_threads (id, org_id, created_by, deleted_at)"
                " VALUES (:t, :o, :u, CASE WHEN :d THEN now() END)"
            ),
            {"t": tid, "o": scope.org, "u": scope.creator, "d": deleted},
        )
    return tid
