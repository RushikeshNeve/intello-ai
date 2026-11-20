"""Unit sanity of the research planner (limits, plan validation) and the markdown escaping."""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.modules.research import planner
from app.modules.research.service import _md


def test_limits_can_only_be_lowered() -> None:
    s = Settings()
    assert planner.effective_limits(s, None) == planner.default_limits(s)
    low = planner.effective_limits(s, {"max_subqueries": 2, "timeout_seconds": None})
    assert low["max_subqueries"] == 2
    assert low["timeout_seconds"] == s.RESEARCH_TIMEOUT_SECONDS
    for bad in ({"max_subqueries": s.RESEARCH_MAX_SUBQUERIES + 1}, {"max_evidence": 0},
                {"max_model_calls": 1}):  # fmt: skip
        with pytest.raises(planner.LimitExceeded):
            planner.effective_limits(s, bad)
    assert planner.subquery_budget({"max_subqueries": 6, "max_model_calls": 3}) == 2


def test_plan_is_validated_and_bounded() -> None:
    raw = json.dumps({"subqueries": ["First  one", "first one", 7, "x", "Second", "Third"], "x": 1})
    assert planner.parse_plan(f"```json\n{raw}\n```", 2) == ["First one", "Second"]
    for bad in ("", "no json", "[1, 2]", '{"subqueries": "a"}', '{"subqueries": []}'):
        assert planner.parse_plan(bad, 3) is None


def test_markdown_never_carries_raw_html() -> None:
    out = _md("a <script>x</script> & <b>y</b>\x00")
    assert "<" not in out
    assert ">" not in out
    assert "\x00" not in out
