"""Tenancy coverage of the M13/M14 Wave A tables, for every role holding SELECT:
``ai_run_captures`` (0020), ``answer_feedback`` (0021), ``firewall_policies`` (0023),
``document_findings`` / ``document_scan_policies`` / ``document_scan_releases`` (0024),
``security_events`` / ``canary_documents`` / ``abuse_settings`` (0025) and
``workspace_pii_settings`` (0027). All are keyed by ``org_id = app.current_org_id``; some add a
user or workspace-admin condition, which the org owner satisfies.
"""

from __future__ import annotations

import pytest
from rls_chat_world import ChatScope, ChatWorld
from rls_docs_world import DocScope, DocWorld
from rls_world import app_tx, scalar
from sqlalchemy import Engine, text

M13_M14_TABLES: tuple[str, ...] = (
    "ai_run_captures",
    "answer_feedback",
    "firewall_policies",
    "document_findings",
    "document_scan_policies",
    "document_scan_releases",
    "security_events",
    "canary_documents",
    "abuse_settings",
    "workspace_pii_settings",
)
# answer_feedback is API-only (the worker role has no grant on it).
CASES = [("app", t) for t in M13_M14_TABLES] + [
    ("worker", t) for t in M13_M14_TABLES if t != "answer_feedback"
]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]


def _seed(admin: Engine, c: ChatScope, d: DocScope) -> None:
    t = c.tenant
    p = {
        "o": t.org_id,
        "w": t.workspace_id,
        "u": t.owner_id,
        "r": c.run,
        "m": c.message,
        "d": d.doc,
        "v": d.version,
        "d2": d.doc2,
    }
    statements = (
        "INSERT INTO ai_run_captures (org_id, run_id, mode, messages, answer, expires_at)"
        " VALUES (:o, :r, 'redacted', 'q', 'a', now() + interval '1 day')",
        "INSERT INTO answer_feedback (org_id, run_id, message_id, user_id, rating)"
        " VALUES (:o, :r, :m, :u, 'up')",
        "INSERT INTO firewall_policies (org_id) VALUES (:o)",
        "INSERT INTO document_findings"
        " (id, org_id, workspace_id, document_id, version_id, rule_id, severity, location)"
        " VALUES (gen_random_uuid(), :o, :w, :d, :v, 'hidden_instruction', 'high', 'p1')",
        "INSERT INTO document_scan_policies (id, org_id, workspace_id, mode, updated_by)"
        " VALUES (gen_random_uuid(), :o, NULL, 'flag', :u)",
        "INSERT INTO document_scan_releases (version_id, org_id, workspace_id, document_id,"
        " released_by) VALUES (:v, :o, :w, :d, :u)",
        "INSERT INTO security_events (id, org_id, kind, severity)"
        " VALUES (gen_random_uuid(), :o, 'abuse_anomaly', 'low')",
        "INSERT INTO canary_documents (org_id, document_id) VALUES (:o, :d2)",
        "INSERT INTO abuse_settings (org_id) VALUES (:o)",
        "INSERT INTO workspace_pii_settings (workspace_id, org_id, updated_by) VALUES (:w, :o, :u)",
    )
    with admin.begin() as conn:
        for sql in statements:
            conn.execute(text(sql), p)


@pytest.fixture(scope="module")
def m13_m14(admin_engine: Engine, chat: ChatWorld, docs: DocWorld) -> tuple[ChatScope, ChatScope]:
    _seed(admin_engine, chat.a, docs.a)
    _seed(admin_engine, chat.b, docs.b)
    return chat.a, chat.b


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_m13m14_select_without_context_sees_nothing(
    engines: dict[str, Engine], m13_m14: tuple[ChatScope, ChatScope], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_m13m14_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], m13_m14: tuple[ChatScope, ChatScope], role: str, table: str
) -> None:
    a, b = (c.tenant for c in m13_m14)
    with app_tx(engines[role], org=a.org_id, user=a.owner_id) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") >= 1
        assert scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": b.org_id}) == 0


def test_m13m14_app_writes_cannot_target_another_org(
    engines: dict[str, Engine], m13_m14: tuple[ChatScope, ChatScope]
) -> None:
    a, b = (c.tenant for c in m13_m14)
    with app_tx(engines["app"], org=a.org_id, user=a.owner_id) as conn:
        for table in ("ai_run_captures", "canary_documents"):
            gone = conn.execute(
                text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": b.org_id}
            ).rowcount
            assert gone == 0, table
        for table, col in (("firewall_policies", "threshold"), ("abuse_settings", "action")):
            moved = conn.execute(
                text(f"UPDATE {table} SET {col} = {col} WHERE org_id = :o"), {"o": b.org_id}
            ).rowcount
            assert moved == 0, table
