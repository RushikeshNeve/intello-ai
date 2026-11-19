"""Make the evaluation modules (``loader``, ``metrics``, ...) and ``app.*`` importable."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[1] / "apps" / "api"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
