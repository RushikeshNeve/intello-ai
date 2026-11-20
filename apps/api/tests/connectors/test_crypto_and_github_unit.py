"""Sanity tests (no database): token encryption (M06.01.03) and the GitHub connector's rules."""

from __future__ import annotations

import base64
import logging
import os

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.modules.connectors import crypto
from app.modules.connectors.github import GitHubConnector
from app.modules.connectors.port import ConnectorError

TOKEN = "ghp_super_secret_token_value"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _settings(**values: object) -> Settings:
    return Settings(**values)  # type: ignore[arg-type]


def test_round_trip_wrong_key_aad_and_no_plaintext() -> None:
    cipher = crypto.cipher_from_settings(_settings(CONNECTOR_ENCRYPTION_KEY=SecretStr(_key())))
    sealed = cipher.seal(TOKEN, aad=b"acct-1")
    assert TOKEN.encode() not in sealed.ciphertext
    assert sealed.key_id == "k1"
    assert cipher.open(sealed.ciphertext, sealed.key_id, aad=b"acct-1") == TOKEN
    assert cipher.seal(TOKEN, aad=b"acct-1").ciphertext != sealed.ciphertext  # fresh nonce
    other = crypto.cipher_from_settings(_settings(CONNECTOR_ENCRYPTION_KEY=SecretStr(_key())))
    with pytest.raises(crypto.DecryptionFailed):
        other.open(sealed.ciphertext, "k1", aad=b"acct-1")
    with pytest.raises(crypto.DecryptionFailed):  # a ciphertext moved to another row
        cipher.open(sealed.ciphertext, "k1", aad=b"acct-2")
    assert TOKEN not in repr(cipher)
    assert TOKEN not in str(crypto.DecryptionFailed("x"))


def test_rotation_keeps_old_rows_readable_and_placeholder_is_rejected() -> None:
    old_key = _key()
    old = crypto.cipher_from_settings(_settings(CONNECTOR_ENCRYPTION_KEY=SecretStr(old_key)))
    sealed = old.seal(TOKEN)
    rotated = crypto.cipher_from_settings(
        _settings(
            CONNECTOR_ENCRYPTION_KEY=SecretStr(_key()),
            CONNECTOR_ENCRYPTION_KEY_ID="k2",
            CONNECTOR_ENCRYPTION_OLD_KEYS=SecretStr(f"k1:{old_key}"),
        )
    )
    assert rotated.open(sealed.ciphertext, "k1") == TOKEN
    assert rotated.seal(TOKEN).key_id == "k2"
    with pytest.raises(crypto.CryptoNotConfigured):
        crypto.cipher_from_settings(
            _settings(CONNECTOR_ENCRYPTION_KEY=SecretStr("change-me-connector-key"))
        )
    with pytest.raises(crypto.CryptoNotConfigured):
        crypto.cipher_from_settings(_settings())


def test_secret_never_reaches_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    cipher = crypto.cipher_from_settings(_settings(CONNECTOR_ENCRYPTION_KEY=SecretStr(_key())))
    sealed = cipher.seal(TOKEN)
    with pytest.raises(crypto.DecryptionFailed):
        cipher.open(sealed.ciphertext, "unknown")
    assert TOKEN not in caplog.text


def _connector(handler: httpx.MockTransport) -> GitHubConnector:
    client = httpx.Client(base_url="https://api.test", transport=handler)
    return GitHubConnector(
        client, {"repo": "o/r", "include": []}, max_file_bytes=1000, max_files=10
    )


def test_config_validation() -> None:
    ok = GitHubConnector.validate_config({"repo": "o/r", "include": ["docs/*"]})
    assert ok["repo"] == "o/r"
    for bad in (
        {"repo": "nope"},
        {"repo": "o/r", "ref": "a..b"},
        {"repo": "o/r", "include": ["/x"]},
    ):
        with pytest.raises(ValueError, match=r"repo|ref|include"):
            GitHubConnector.validate_config(bad)


def test_rate_limit_stops_with_a_reschedule_hint_and_filters_apply() -> None:
    sha = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/HEAD"):
            return httpx.Response(200, json={"sha": sha})
        if "/git/trees/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"type": "blob", "path": "README.md", "sha": "1" * 40, "size": 10},
                        {"type": "blob", "path": "node_modules/x.js", "sha": "2" * 40, "size": 10},
                        {"type": "blob", "path": "logo.png", "sha": "3" * 40, "size": 10},
                        {"type": "blob", "path": "big.md", "sha": "4" * 40, "size": 5000},
                    ],
                },
            )
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    connector = _connector(httpx.MockTransport(handler))
    page = connector.discover(None)
    assert [i.path for i in page.items] == ["README.md"]
    assert page.full_listing
    assert page.next_cursor == sha
    with pytest.raises(ConnectorError) as info:
        connector.fetch(page.items[0])
    assert info.value.code == "rate_limited"
    assert (info.value.retry_after_seconds or 0) >= 60
