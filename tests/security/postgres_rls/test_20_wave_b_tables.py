"""Tenancy coverage of the M11/M12 Wave B tables, for every role holding SELECT:
``usage_limits`` (0015, M11.08), ``prompt_gate_settings`` (0019a, M12.06), ``shadow_candidates`` and
``shadow_runs`` (0019c, M12.09). All are keyed by ``org_id = app.current_org_id``.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from rls_chat_world import ChatWorld
from rls_world import Tenant, World, app_tx, scalar
from sqlalchemy import Engine, text

WAVE_B_TABLES: tuple[str, ...] = (
    "usage_limits",
    "prompt_gate_settings",
    "shadow_candidates",
    "shadow_runs",
)
# prompt_gate_settings is API-only (the worker role has no grant on it).
CASES = [("app", t) for t in WAVE_B_TABLES] + [
    ("worker", t) for t in WAVE_B_TABLES if t != "prompt_gate_settings"
]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]
SHA = hashlib.sha256(b"wave-b").hexdigest()


def _seed(admin: Engine, t: Tenant, run_id: uuid.UUID) -> None:
    version = uuid.uuid4()
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO prompt_versions (id, org_id, kind, version, body, sha256, state)"
                " VALUES (:i, :o, 'chat_system', 950, 'waveb-shadow', :h, 'draft')"
            ),
            {"i": version, "o": t.org_id, "h": SHA},
        )
        conn.execute(
            text(
                "INSERT INTO usage_limits (id, org_id, scope, kind, period, max_requests, action)"
                " VALUES (:i, :o, 'org', 'chat', 'day', 5, 'warn')"
            ),
            {"i": uuid.uuid4(), "o": t.org_id},
        )
        conn.execute(text("INSERT INTO prompt_gate_settings (org_id) VALUES (:o)"), {"o": t.org_id})
        conn.execute(
            text(
                "INSERT INTO shadow_candidates (org_id, kind, version_id, rate_pct)"
                " VALUES (:o, 'chat_system', :v, 5)"
            ),
            {"o": t.org_id, "v": version},
        )
        conn.execute(
            text(
                "INSERT INTO shadow_runs (id, org_id, run_id, candidate_version_id)"
                " VALUES (:i, :o, :r, :v)"
            ),
            {"i": uuid.uuid4(), "o": t.org_id, "r": run_id, "v": version},
        )


@pytest.fixture(scope="module")
def wave_b(admin_engine: Engine, world: World, chat: ChatWorld) -> tuple[Tenant, Tenant]:
    _seed(admin_engine, world.a, chat.a.run)
    _seed(admin_engine, world.b, chat.b.run)
    return world.a, world.b


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_waveb_select_without_context_sees_nothing(
    engines: dict[str, Engine], wave_b: tuple[Tenant, Tenant], role: str, table: str
) -> None:
    with app_tx(engines[role]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_waveb_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], wave_b: tuple[Tenant, Tenant], role: str, table: str
) -> None:
    a, b = wave_b
    with app_tx(engines[role], org=a.org_id, user=a.owner_id) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") >= 1
        assert scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": b.org_id}) == 0


def test_waveb_writes_cannot_target_another_org(
    engines: dict[str, Engine], wave_b: tuple[Tenant, Tenant]
) -> None:
    a, b = wave_b
    with app_tx(engines["app"], org=a.org_id, user=a.owner_id) as conn:
        for table in ("usage_limits", "prompt_gate_settings", "shadow_candidates"):
            moved = conn.execute(
                text(f"UPDATE {table} SET org_id = org_id WHERE org_id = :o"), {"o": b.org_id}
            ).rowcount
            assert moved == 0, table
