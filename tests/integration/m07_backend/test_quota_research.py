"""M11.08: a research job over its quota fails safely (terminal ``quota_exceeded``, no retry)."""

from __future__ import annotations

from catalog_support import World
from ret_support import Rig
from sqlalchemy import Engine
from test_research import GOOD, Env, plan_json, scripted

from app.core.config import Settings


def test_research_over_quota_fails_with_a_stable_code(
    world: World, rig: Rig, worker_engine: Engine, base_settings: Settings
) -> None:
    env = Env(world, rig, worker_engine, base_settings)
    env.publish()
    res = world.api.post(
        f"/v1/orgs/{world.org_id}/usage/limits",
        world.t.admin,
        {"kind": "research", "max_requests": 0, "action": "block"},
    )
    assert res.status == 201, res.body
    session = env.start()
    assert env.run(session, scripted(plan_json("herons"), GOOD)) == "failed"
    done = env.detail(session)
    assert (done["state"], done["error_code"]) == ("failed", "quota_exceeded")
