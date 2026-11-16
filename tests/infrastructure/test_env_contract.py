"""M00.01.02 acceptance: every service variable is documented with a safe placeholder.

Checks `infrastructure/env/.env.example` against every compose file:
* every ${VAR} a compose file interpolates is defined in the contract;
* every variable sits under a commented section, and secrets carry a `change-me-` placeholder;
* nothing in the contract looks like a real credential, and host ports bind to loopback by default.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "infrastructure" / "env" / ".env.example"
COMPOSE_FILES = sorted((ROOT / "infrastructure" / "compose").glob("*.yml")) + [ROOT / "compose.yaml"]

ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
INTERP = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?:[:?-][^}]*)?\}")
SECRET_NAME = re.compile(r"(PASSWORD|SECRET|TOKEN|ACCESS_KEY_ID)$")
# Variables Compose itself or the container images provide; they are not part of our contract.
COMPOSE_BUILTINS: set[str] = set()


def _contract() -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    for line in lines:
        m = ASSIGN.match(line)
        if m:
            assert m.group(1) not in values, f"duplicate variable {m.group(1)}"
            values[m.group(1)] = m.group(2)
    return values, lines


def test_compose_files_exist() -> None:
    assert any(p.name == "data.yml" for p in COMPOSE_FILES)
    assert all(p.exists() for p in COMPOSE_FILES)


def test_every_interpolated_variable_is_in_the_contract() -> None:
    values, _ = _contract()
    missing: dict[str, list[str]] = {}
    for path in COMPOSE_FILES:
        for name in set(INTERP.findall(path.read_text(encoding="utf-8"))) - COMPOSE_BUILTINS:
            if name not in values:
                missing.setdefault(name, []).append(path.name)
    assert not missing, f"variables used by compose but not documented in .env.example: {missing}"


def test_every_variable_is_documented_under_a_comment() -> None:
    _, lines = _contract()
    undocumented = []
    for i, line in enumerate(lines):
        m = ASSIGN.match(line)
        if not m:
            continue
        # Walk back over the contiguous block of assignments/comments: a comment must precede the variable
        # within its section (sections are separated by blank lines).
        j = i - 1
        while j >= 0 and lines[j].strip() and not lines[j].startswith("#"):
            j -= 1
        if j < 0 or not lines[j].startswith("#"):
            undocumented.append(m.group(1))
    assert not undocumented, f"variables without a documenting comment: {undocumented}"


def test_secrets_use_safe_placeholders() -> None:
    values, _ = _contract()
    bad = {k: v for k, v in values.items() if SECRET_NAME.search(k) and not v.startswith("change-me-")}
    assert not bad, f"secret variables must use change-me-* placeholders: {sorted(bad)}"


def test_connection_urls_only_embed_placeholder_credentials() -> None:
    values, _ = _contract()
    for k, v in values.items():
        m = re.match(r"^[a-z+]+://([^@/]*)@", v)
        if m and ":" in m.group(1):
            password = m.group(1).split(":", 1)[1]
            assert password.startswith("change-me-"), f"{k} embeds a non-placeholder password"


def test_no_real_looking_credentials() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    for pattern in (r"AKIA[0-9A-Z]{16}", r"sk-[A-Za-z0-9]{20,}", r"ghp_[A-Za-z0-9]{20,}", r"-----BEGIN"):
        assert not re.search(pattern, text), f"credential-like value matching {pattern}"


def test_loopback_binding_and_no_paid_providers_by_default() -> None:
    values, _ = _contract()
    assert values["UKOS_BIND_HOST"] == "127.0.0.1"
    assert values["AI_EXTERNAL_PROVIDERS_ENABLED"] == "false"
    for path in COMPOSE_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"^\s*-\s*\"?\$\{UKOS_BIND_HOST", line) is None and re.search(r"^\s*-\s*\"?\d+:\d+", line):
                raise AssertionError(f"{path.name}: port published without UKOS_BIND_HOST: {line.strip()}")
