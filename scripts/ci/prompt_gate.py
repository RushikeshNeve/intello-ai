#!/usr/bin/env python3
"""Prompt change gate (M12.06). Fails when prompt defaults or the fixed scaffold changed without
(1) a new `docs/release/PROMPT_CHANGELOG.md` line naming each changed kind and (2) a passing eval gate.

  python scripts/ci/prompt_gate.py --base origin/main          # CI: diff against the base ref
  python scripts/ci/prompt_gate.py --changed F --added A       # fixture mode: file lists, no git
Exit 0 = pass / nothing to gate, 1 = gate failed, 2 = usage error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFAULTS_DIR = "apps/api/app/modules/prompts/defaults/"
SCAFFOLD = "apps/api/app/modules/prompts/scaffold.py"
CHANGELOG = "docs/release/PROMPT_CHANGELOG.md"
KINDS = ("chat_system", "graph_extract", "research_plan")
EVAL_CMD = "cd apps/api && python ../../tests/evaluation/run_eval.py --require-baseline --metrics"


def changed_kinds(files: list[str]) -> set[str]:
    kinds: set[str] = set()
    for f in files:
        f = f.replace("\\", "/")
        if f == SCAFFOLD:
            kinds.update(KINDS)
        elif f.startswith(DEFAULTS_DIR):
            stem = Path(f).stem
            kinds.update((stem,) if stem in KINDS else KINDS)
    return kinds


def missing_changelog(kinds: set[str], added_lines: list[str]) -> list[str]:
    text = "\n".join(added_lines)
    return sorted(k for k in kinds if not re.search(rf"\b{re.escape(k)}\b", text))


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="git base ref (diffs base...HEAD)")
    ap.add_argument("--changed", help="file with changed paths, one per line (fixture mode)")
    ap.add_argument("--added", help="file with lines added to the changelog (fixture mode)")
    ap.add_argument("--eval-cmd", default=EVAL_CMD, help="eval gate command; exit 0 = pass")
    a = ap.parse_args(argv)
    if a.changed is not None:
        files = Path(a.changed).read_text(encoding="utf-8").split()
        added = Path(a.added).read_text(encoding="utf-8").splitlines() if a.added else []
    elif a.base:
        files = _git("diff", "--name-only", f"{a.base}...HEAD").split()
        diff = _git("diff", "-U0", f"{a.base}...HEAD", "--", CHANGELOG).splitlines()
        added = [x[1:] for x in diff if x.startswith("+") and not x.startswith("+++")]
    else:
        ap.error("need --base or --changed")
    kinds = changed_kinds(files)
    if not kinds:
        print("prompt gate: no prompt defaults or scaffold changes")
        return 0
    missing = missing_changelog(kinds, added)
    if missing:
        print(f"prompt gate FAIL: {CHANGELOG} needs a new entry naming: {', '.join(missing)}")
        return 1
    if subprocess.run(a.eval_cmd, shell=True).returncode != 0:  # noqa: S602
        print("prompt gate FAIL: eval gate (--require-baseline --metrics) did not pass")
        return 1
    print(f"prompt gate PASS: {', '.join(sorted(kinds))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
