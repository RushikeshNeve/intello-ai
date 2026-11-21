"""M12.06: activation gate decisions (no database) and the CI gate script with fixture diffs."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.modules.prompts import activate as gate
from app.modules.prompts import service

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("prompt_gate", ROOT / "scripts/ci/prompt_gate.py")
assert _spec
assert _spec.loader
ci: Any = importlib.util.module_from_spec(_spec)
sys.modules["prompt_gate"] = ci
_spec.loader.exec_module(ci)

VERSION = {"kind": "chat_system", "sha256": "a" * 64}
RUN = uuid.uuid4()


def _session(settings: tuple[Any, ...] | None, run: tuple[Any, ...] | None) -> MagicMock:
    s = MagicMock()
    s.execute.side_effect = [
        MagicMock(one_or_none=lambda: settings),
        MagicMock(one_or_none=lambda: run),
    ]
    return s


def test_gate_off_never_blocks() -> None:
    org = uuid.uuid4()
    assert gate.check_gate(_session(None, None), org, VERSION, None, None) == (None, None)
    assert gate.check_gate(_session((False, 0.8), None), org, VERSION, None, None) == (None, None)


def test_gate_on_requires_passing_run_or_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    org = uuid.uuid4()
    with pytest.raises(gate.EvalRequired):
        gate.check_gate(_session((True, 0.8), None), org, VERSION, None, None)
    with pytest.raises(gate.EvalRequired):  # failing run
        gate.check_gate(
            _session((True, 0.8), ("completed", {"pass_rate": 0.5})), org, VERSION, RUN, None
        )
    ok = gate.check_gate(
        _session((True, 0.8), ("completed", {"pass_rate": 0.9})), org, VERSION, RUN, None
    )
    assert ok == (RUN, None)
    monkeypatch.setenv(gate.ATTESTATION_ENV, "s3cret")
    att = gate.sign_attestation("chat_system", VERSION["sha256"], "s3cret")
    assert gate.check_gate(_session((True, 0.8), None), org, VERSION, None, att) == (None, att)
    with pytest.raises(gate.EvalRequired):
        gate.check_gate(_session((True, 0.8), None), org, VERSION, None, att + "x")


def test_rollback_is_never_gated() -> None:
    assert "check_gate" not in inspect.getsource(service)  # rollback -> service.activate


def test_ci_changed_kinds_and_changelog() -> None:
    d = "apps/api/app/modules/prompts/defaults/"
    assert ci.changed_kinds([d + "chat_system.txt", "README.md"]) == {"chat_system"}
    assert ci.changed_kinds(["apps/api/app/modules/prompts/scaffold.py"]) == set(ci.KINDS)
    assert ci.changed_kinds(["apps/web/x.ts"]) == set()
    assert ci.missing_changelog({"chat_system"}, ["- tweak chat_system tone"]) == []
    assert ci.missing_changelog({"chat_system"}, ["- unrelated"]) == ["chat_system"]


def test_ci_script_fixture_diff(tmp_path: Path) -> None:
    changed = tmp_path / "c.txt"
    changed.write_text("apps/api/app/modules/prompts/defaults/graph_extract.txt\n")
    added = tmp_path / "a.txt"
    added.write_text("- graph_extract: tighter schema\n")
    base = ["--changed", str(changed), "--added", str(added)]
    with patch.object(ci.subprocess, "run", return_value=MagicMock(returncode=0)):
        assert ci.main(base) == 0
    with patch.object(ci.subprocess, "run", return_value=MagicMock(returncode=1)):
        assert ci.main(base) == 1  # eval gate failed
    added.write_text("- nothing relevant\n")
    assert ci.main(base) == 1  # no changelog entry
