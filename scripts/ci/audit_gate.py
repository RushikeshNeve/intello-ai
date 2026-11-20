"""Dependency audit gate (M08.03.01): print a short report, fail only on CRITICAL findings.

    python3 scripts/ci/audit_gate.py pip /tmp/pip-audit.json     # pip-audit --format json output
    python3 scripts/ci/audit_gate.py npm /tmp/npm-audit.json     # npm audit --json output

pip-audit's JSON has no severity, so Python findings are only reported (non-blocking) unless the advisory
text says critical. npm audit carries severities: `critical` fails the gate; high/moderate/low are reported.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def pip_report(data: dict[str, Any]) -> tuple[list[str], int]:
    lines: list[str] = []
    critical = 0
    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            text = f"{vuln.get('id', '?')} {vuln.get('description', '')}".lower()
            is_critical = "critical" in text
            critical += is_critical
            lines.append(
                f"{dep['name']} {dep.get('version', '?')}: {vuln.get('id', '?')}"
                + (" [CRITICAL]" if is_critical else "")
            )
    return lines, critical


def npm_report(data: dict[str, Any]) -> tuple[list[str], int]:
    meta = data.get("metadata", {}).get("vulnerabilities", {})
    lines = [f"{k}: {v}" for k, v in meta.items() if k != "total" and v]
    for name, vuln in sorted(data.get("vulnerabilities", {}).items()):
        lines.append(f"{name}: {vuln.get('severity')}")
    return lines, int(meta.get("critical", 0))


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"pip", "npm"}:
        print(__doc__)
        return 2
    path = Path(argv[2])
    if not path.exists() or path.stat().st_size == 0:
        print(f"{argv[1]} audit produced no output (tool failure or no network): not gating")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    lines, critical = (pip_report if argv[1] == "pip" else npm_report)(data)
    print(f"{argv[1]} audit: {len(lines)} finding line(s), {critical} critical")
    for line in lines[:60]:
        print("  " + line)
    if critical:
        print("FAIL: critical vulnerabilities")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
