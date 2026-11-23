"""M14.08 unit: master-key wrap/unwrap, context binding, KEK rotation, stubs, no key echo."""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.crypto.envelope import wrap_context
from app.core.crypto.errors import CryptoNotConfigured, DecryptionFailed
from app.core.crypto.providers import (
    LocalFileKeyProvider,
    RemoteKmsKeyProvider,
    envelope_enabled,
    provider_from_settings,
)

K1 = base64.b64encode(bytes(range(32))).decode()
K2 = base64.b64encode(bytes(range(1, 33))).decode()


def _settings(**over: object) -> Settings:
    return Settings().model_copy(update=over)


def test_wrap_unwrap_round_trip_bound_to_context(tmp_path: Path) -> None:
    key_file = tmp_path / "kek"
    key_file.write_text(K1, encoding="utf-8")
    settings = _settings(MASTER_KEY_FILE=str(key_file))
    assert envelope_enabled(settings)
    provider = provider_from_settings(settings)
    dek, org, dek_id = os.urandom(32), uuid.uuid4(), uuid.uuid4()
    kek_id, wrapped = provider.wrap(dek, context=wrap_context(org, dek_id))
    assert kek_id == "kek1"
    assert dek not in wrapped
    assert provider.unwrap(wrapped, kek_id, context=wrap_context(org, dek_id)) == dek
    with pytest.raises(DecryptionFailed):  # copied to another org
        provider.unwrap(wrapped, kek_id, context=wrap_context(uuid.uuid4(), dek_id))
    with pytest.raises(DecryptionFailed):  # unknown KEK id
        provider.unwrap(wrapped, "nope", context=wrap_context(org, dek_id))


def test_old_kek_unwraps_after_rotation_and_repr_hides_keys() -> None:
    old = provider_from_settings(_settings(MASTER_KEY=SecretStr(K1)))
    ctx = wrap_context(uuid.uuid4(), uuid.uuid4())
    kid, wrapped = old.wrap(b"\x07" * 32, context=ctx)
    new = provider_from_settings(
        _settings(
            MASTER_KEY=SecretStr(K2), MASTER_KEY_ID="kek2", MASTER_OLD_KEYS=SecretStr(f"{kid}:{K1}")
        )
    )
    assert new.active_key_id == "kek2"
    assert new.unwrap(wrapped, kid, context=ctx) == b"\x07" * 32
    assert new.wrap(b"\x07" * 32, context=ctx)[0] == "kek2"
    assert K1 not in repr(new)
    assert K2 not in repr(new)


def test_misconfiguration_and_stub_providers_fail_closed_without_echo() -> None:
    assert not envelope_enabled(Settings())
    with pytest.raises(CryptoNotConfigured) as bad:
        provider_from_settings(_settings(MASTER_KEY=SecretStr("short-secret-value")))
    assert "short-secret-value" not in str(bad.value)
    with pytest.raises(CryptoNotConfigured):
        provider_from_settings(_settings(MASTER_KEY_FILE="/nonexistent/kek"))
    with pytest.raises(CryptoNotConfigured):
        LocalFileKeyProvider({"a": b"x" * 32}, "b")
    stub = provider_from_settings(_settings(KEY_PROVIDER="aws-kms", MASTER_KEY=SecretStr(K1)))
    assert isinstance(stub, RemoteKmsKeyProvider)
    with pytest.raises(CryptoNotConfigured):
        stub.wrap(b"\x00" * 32, context=b"")
