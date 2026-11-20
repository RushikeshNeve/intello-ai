"""Connector side of the RLS world (M06 tables, migration 0009) and its grant registry.

Seeds, as the admin role, per tenant of :class:`rls_world.World`: one connector account, one
connector source on the tenant's workspace, one sync run and one item. ``CONNECTOR_GRANTS`` is the
hand-written expectation of what each least-privilege role may do on each table;
``test_18_connector_tables.py`` cross-checks it against the live catalog.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from rls_docs_world import Grant, _g
from rls_world import Tenant, World
from sqlalchemy import Engine, text

CONNECTOR_TABLES: tuple[str, ...] = ("connector_accounts", "source_syncs", "source_items")
ROLES: tuple[str, ...] = ("app", "worker")

CONNECTOR_GRANTS: dict[str, dict[str, Grant]] = {
    "app": {
        "connector_accounts": _g(True, True, "token_ciphertext key_id revoked_at"),
        "source_syncs": _g(True, True, "state error_code finished_at"),
        "source_items": _g(True, False, "state"),
    },
    "worker": {
        "connector_accounts": _g(True),
        "source_syncs": _g(
            True, False, "state cursor_before cursor_after stats error_code started_at finished_at"
        ),
        "source_items": _g(
            True, True, "path document_id version_token content_sha256 state last_seen_at"
        ),
    },
}

CASES: list[tuple[str, str]] = [(r, t) for r in ROLES for t in CONNECTOR_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]


@dataclass(frozen=True)
class ConnectorScope:
    tenant: Tenant
    account: uuid.UUID
    source: uuid.UUID
    sync: uuid.UUID
    item: uuid.UUID

    @property
    def org(self) -> uuid.UUID:
        return self.tenant.org_id

    @property
    def ws(self) -> uuid.UUID:
        return self.tenant.workspace_id

    def pk(self, table: str) -> uuid.UUID:
        return {
            "connector_accounts": self.account,
            "source_syncs": self.sync,
            "source_items": self.item,
        }[table]


@dataclass(frozen=True)
class ConnectorWorld:
    a: ConnectorScope
    b: ConnectorScope


def _seed(conn: Any, t: Tenant) -> ConnectorScope:
    account, source, sync, item = (uuid.uuid4() for _ in range(4))
    conn.execute(
        text(
            "INSERT INTO connector_accounts (id, org_id, provider, label, created_by)"
            " VALUES (:i, :o, 'github', :l, :u)"
        ),
        {"i": account, "o": t.org_id, "l": f"acct-{t.marker}", "u": t.owner_id},
    )
    conn.execute(
        text(
            "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by,"
            " connector_account_id) VALUES (:i, :o, :w, 'connector', :n, :u, :a)"
        ),
        {
            "i": source,
            "o": t.org_id,
            "w": t.workspace_id,
            "n": f"src-{t.marker}",
            "u": t.owner_id,
            "a": account,
        },
    )
    conn.execute(
        text(
            "INSERT INTO source_syncs (id, org_id, source_id, trigger) VALUES (:i, :o, :s, 'manual')"
        ),
        {"i": sync, "o": t.org_id, "s": source},
    )
    conn.execute(
        text(
            "INSERT INTO source_items (id, org_id, source_id, remote_id, path)"
            " VALUES (:i, :o, :s, 'README.md', 'README.md')"
        ),
        {"i": item, "o": t.org_id, "s": source},
    )
    return ConnectorScope(t, account, source, sync, item)


def seed_connector_world(engine: Engine, world: World) -> ConnectorWorld:
    with engine.begin() as conn:
        a = _seed(conn, world.a)
        b = _seed(conn, world.b)
    return ConnectorWorld(a, b)
