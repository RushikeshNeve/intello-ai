"""Unit tests for audit metadata redaction (contract §5)."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from app.modules.audit.redaction import (
    MAX_DEPTH,
    MAX_ITEMS,
    MAX_NODES,
    MAX_STRING_LENGTH,
    REDACTED,
    TRUNCATED,
    TRUNCATED_KEY,
    is_sensitive_key,
    redact,
)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "PASSWD",
        "passphrase",
        "client_secret",
        "token",
        "access_token",
        "refreshToken",
        "id_token",
        "X-Api-Key",
        "api_key",
        "apiKey",
        "Authorization",
        "authorization",
        "Cookie",
        "Set-Cookie",
        "privateKey",
    ],
)
def test_sensitive_keys_redacted_case_insensitive(key: str) -> None:
    assert is_sensitive_key(key)
    assert redact({key: "value-123", "role": "admin"}) == {key: REDACTED, "role": "admin"}


@pytest.mark.parametrize("key", ["email", "role", "user_id", "from", "to", "reason", "action"])
def test_ordinary_keys_kept(key: str) -> None:
    assert not is_sensitive_key(key)
    assert redact({key: "v"}) == {key: "v"}


def test_nested_dicts_and_lists() -> None:
    metadata = {
        "request": {
            "headers": {"X-Api-Key": "k", "Authorization": "Bearer abc", "Accept": "json"},
            "items": [{"refreshToken": "r", "name": "n"}, ["x", {"secret": {"deep": 1}}]],
        },
        "tokens": ["a", "b"],
    }
    assert redact(metadata) == {
        "request": {
            "headers": {"X-Api-Key": REDACTED, "Authorization": REDACTED, "Accept": "json"},
            "items": [{"refreshToken": REDACTED, "name": "n"}, ["x", {"secret": REDACTED}]],
        },
        "tokens": REDACTED,
    }


def test_strings_truncated_to_512() -> None:
    out = redact({"note": "x" * 2000, "nested": {"list": ["y" * 600]}})
    assert out["note"] == "x" * MAX_STRING_LENGTH
    assert len(out["nested"]["list"][0]) == MAX_STRING_LENGTH
    assert redact({"note": "z" * 512})["note"] == "z" * 512


def test_emails_preserved() -> None:
    metadata = {"email": "Alice.Smith+ops@example.com", "members": [{"email": "bob@example.org"}]}
    assert redact(metadata) == metadata


def test_input_not_mutated() -> None:
    metadata: dict[str, Any] = {
        "password": "p",
        "nested": {"token": "t", "list": [{"cookie": "c"}, "x" * 1000]},
    }
    before = copy.deepcopy(metadata)
    out = redact(metadata)
    assert metadata == before
    assert out is not metadata
    assert out["nested"] is not metadata["nested"]


def test_credential_looking_values_redacted_under_any_key() -> None:
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    out = redact({"note": jwt, "header": "Bearer abc.def-ghi", "plan": "basic plan"})
    assert out == {"note": REDACTED, "header": REDACTED, "plan": "basic plan"}


class Color(Enum):
    RED = "red"


class _Odd:
    def __str__(self) -> str:
        return "odd"


class _Hostile:
    def __str__(self) -> str:
        raise RuntimeError("boom")


def test_non_json_values_stringified() -> None:
    ident = uuid.uuid4()
    when = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    metadata: dict[Any, Any] = {
        "id": ident,
        "when": when,
        "amount": Decimal("1.50"),
        "color": Color.RED,
        "obj": _Odd(),
        "hostile": _Hostile(),
        "raw": b"secret-bytes",
        "nan": float("nan"),
        "tags": {"a"},
        "tuple": (1, 2),
        "ok": [None, True, 3, 1.5],
        1: "int key",
    }
    out = redact(metadata)
    assert out["id"] == str(ident)
    assert out["when"] == "2026-11-16T12:00:00+00:00"
    assert out["amount"] == "1.50"
    assert out["color"] == "red"
    assert out["obj"] == "odd"
    assert out["hostile"] == "<unprintable _Hostile>"
    assert out["raw"] == "[BINARY 12 bytes]"
    assert out["nan"] == "nan"
    assert out["tags"] == ["a"]
    assert out["tuple"] == [1, 2]
    assert out["ok"] == [None, True, 3, 1.5]
    assert out["1"] == "int key"
    json.dumps(out, allow_nan=False)


def test_depth_bounded_and_cycles_safe() -> None:
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(MAX_DEPTH + 5):
        node["child"] = {}
        node = node["child"]
    level: Any = redact(deep)
    for _ in range(MAX_DEPTH):
        level = level["child"]
    assert level == TRUNCATED

    cyclic: dict[str, Any] = {"name": "loop"}
    cyclic["self"] = cyclic
    json.dumps(redact(cyclic))


def test_size_bounded() -> None:
    out = redact({f"k{i}": i for i in range(MAX_ITEMS + 25)})
    assert len(out) == MAX_ITEMS + 1
    assert out[TRUNCATED_KEY] == 25

    long_list = redact({"l": list(range(MAX_ITEMS + 3))})["l"]
    assert len(long_list) == MAX_ITEMS + 1
    assert long_list[-1] == f"{TRUNCATED} 3 more"

    huge = {f"a{i}": {f"b{j}": j for j in range(50)} for i in range(50)}
    out = redact(huge)
    encoded = json.dumps(out)
    assert out[TRUNCATED_KEY] > 0
    assert encoded.count(":") <= MAX_NODES + MAX_DEPTH

    assert list(redact({"k" * 1000: 1})) == ["k" * 128]


def test_none_and_non_mapping() -> None:
    assert redact(None) == {}
    assert redact({}) == {}
    with pytest.raises(TypeError):
        redact(["not", "a", "mapping"])  # type: ignore[arg-type]
