"""Small regex secret scan over git-tracked files (M08.03.01). No dependencies, no network.

Fails on private keys, cloud/API tokens and JWT-shaped strings. The `change-me-*` placeholders, `ukos-dev-*`
test passwords and the other documented dev values are not secrets and are never flagged. To allow a
reviewed false positive append `# secret-scan: allow` to the line.

    python3 scripts/ci/secret_scan.py          # scan tracked files, exit 1 on a finding
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b"
    ),
    "OpenAI-style key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\b"),
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".lock",
    ".svg",
}
ALLOW = "secret-scan: allow"
# Reviewed fixtures: a fake, unsigned JWT used to test that tokens are redacted from error messages.
ALLOWED_FILES = {"apps/web/src/features/auth/__tests__/primitives.test.ts"}
MAX_BYTES = 2_000_000


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,  # noqa: S607
    ).stdout
    return [root / p for p in out.decode().split("\0") if p]


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in tracked_files(root):
        if (
            path.suffix.lower() in SKIP_SUFFIXES
            or not path.is_file()
            or path.stat().st_size > MAX_BYTES
        ):
            continue
        if path.relative_to(root).as_posix() in ALLOWED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), start=1):
            if ALLOW in line:
                continue
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path.relative_to(root).as_posix()}:{n}: {name}")
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    findings = scan(root)
    if findings:
        print("secret scan FAILED (values are not printed):")
        for f in findings[:50]:
            print("  " + f)
        return 1
    print("secret scan ok: no private keys, cloud/API tokens or JWTs in tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
