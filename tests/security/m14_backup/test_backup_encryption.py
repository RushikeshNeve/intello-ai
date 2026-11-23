"""M14.08 encrypted backups: round trip of scripts/ops/backup-crypto.sh on a synthetic backup made by
the real manifest writer (no stack needed). Host or Linux runner: needs bash, openssl, node, tar::

    python -m pytest -q -p no:cacheprovider ../../tests/security/m14_backup
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OPS = REPO / "scripts" / "ops"
CANARY = b"CANARY_m14_backup_row_payload_7c1d"
KEY = "m14-test-backup-key-0123456789abcdefABCDEF"
# On Windows `bash` on PATH may be WSL's; the ops scripts run in Git Bash.
_GIT_BASH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "bash.exe"
BASH = os.environ.get("UKOS_BASH") or (
    str(_GIT_BASH) if sys.platform == "win32" and _GIT_BASH.exists() else "bash"
)

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in (BASH, "node")),
    reason="bash (with openssl, tar) and node required",
)


def _posix(p: Path) -> str:
    return p.as_posix()


def _sh(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", f"set -euo pipefail; source '{_posix(OPS)}/backup-crypto.sh'; {script}"],
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    d = tmp_path / "ukos-backup-x"
    (d / "objects" / "ukos-originals" / "o1").mkdir(parents=True)
    (d / "postgres.dump").write_bytes(b"PGDMP" + CANARY * 50)
    (d / "rushdb-data.tgz").write_bytes(b"\x1f\x8b" + CANARY)
    (d / "rowcounts.txt").write_text("documents=3\n", encoding="utf-8")
    (d / "rushdb-counts.json").write_text("{}", encoding="utf-8")
    (d / "objects" / "ukos-originals" / "o1" / "blob").write_bytes(CANARY)
    subprocess.run(
        ["node", _posix(OPS / "manifest.mjs"), "write", _posix(d), "--migration-head", "0026",
         "--project", "t", "--buckets", "ukos-originals"],
        check=True, capture_output=True,
    )  # fmt: skip
    return d


def _all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def test_encrypted_backup_round_trip_and_failures(backup: Path, tmp_path: Path) -> None:
    key = tmp_path / "backup.key"
    key.write_text(KEY, encoding="utf-8")
    enc = _sh(f"bk_encrypt '{_posix(backup)}' '{_posix(key)}'")
    assert sorted(p.name for p in backup.iterdir()) == ["SHA256SUMS", "manifest.json", "payload.tar.enc"]
    blob = _all_bytes(backup)
    assert CANARY not in blob
    assert KEY.encode() not in blob
    assert KEY not in enc.stdout + enc.stderr
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["encrypted"] is True
    assert manifest["key_id"].startswith("sha256:")
    assert manifest["encryption"]["cipher"].startswith("openssl-aes-256-cbc")
    assert _sh(f"bk_is_encrypted '{_posix(backup)}'").returncode == 0

    out = tmp_path / "plain"
    _sh(f"bk_decrypt '{_posix(backup)}' '{_posix(key)}' '{_posix(out)}'")
    verify = subprocess.run(
        ["node", _posix(OPS / "manifest.mjs"), "verify", _posix(out)], capture_output=True, text=True
    )
    assert verify.returncode == 0, verify.stderr
    assert (out / "objects" / "ukos-originals" / "o1" / "blob").read_bytes() == CANARY

    wrong = tmp_path / "wrong.key"
    wrong.write_text(KEY[::-1], encoding="utf-8")
    bad = _sh(f"bk_decrypt '{_posix(backup)}' '{_posix(wrong)}' '{_posix(tmp_path / 'w')}'", check=False)
    assert bad.returncode != 0
    assert "does not match" in bad.stderr

    payload = backup / "payload.tar.enc"
    data = bytearray(payload.read_bytes())
    data[-5] ^= 0xFF
    payload.write_bytes(bytes(data))
    tampered = _sh(
        f"bk_decrypt '{_posix(backup)}' '{_posix(key)}' '{_posix(tmp_path / 't')}'", check=False
    )
    assert tampered.returncode != 0
    assert "checksum mismatch" in tampered.stderr


def test_short_key_is_refused(backup: Path, tmp_path: Path) -> None:
    key = tmp_path / "short.key"
    key.write_text("too-short", encoding="utf-8")
    res = _sh(f"bk_encrypt '{_posix(backup)}' '{_posix(key)}'", check=False)
    assert res.returncode != 0
    assert (backup / "postgres.dump").exists()  # nothing was removed
