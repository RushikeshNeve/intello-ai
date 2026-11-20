"""OpenAPI contract snapshot (M08.03.01): route or schema changes must be reviewed on purpose.

The committed ``apps/api/openapi.snapshot.json`` is the reviewed contract. This test fails when
the live schema differs. After an INTENDED change, regenerate the snapshot and commit it with
the diff in the review:

    cd apps/api && python -m tests.contract.test_openapi_snapshot --update
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app.main import create_app

UPDATE = "cd apps/api && python -m tests.contract.test_openapi_snapshot --update"
SNAPSHOT = Path(__file__).resolve().parents[2] / "openapi.snapshot.json"


def render() -> str:
    spec: dict[str, Any] = create_app().openapi()
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def test_openapi_matches_reviewed_snapshot() -> None:
    assert SNAPSHOT.exists(), f"missing snapshot; run: {UPDATE}"
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    live = json.loads(render())
    if committed != live:
        old = {f"{m.upper()} {p}" for p, ms in committed["paths"].items() for m in ms}
        new = {f"{m.upper()} {p}" for p, ms in live["paths"].items() for m in ms}
        raise AssertionError(
            "OpenAPI changed without a reviewed snapshot update. "
            f"added routes: {sorted(new - old)[:10]}; removed routes: {sorted(old - new)[:10]}; "
            f"if intended, run: {UPDATE}"
        )


if __name__ == "__main__":
    if "--update" in sys.argv:
        SNAPSHOT.write_text(render(), encoding="utf-8", newline="\n")
        print(f"wrote {SNAPSHOT}")
    else:
        print("pass --update to rewrite the snapshot")
