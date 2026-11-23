"""Master-key (KEK) adapters: wrap and unwrap per-org data keys.

``KeyProvider`` is the only interface the rest of the code sees. ``context`` is bound into the
wrap (AES-GCM associated data / KMS encryption context) so a wrapped DEK copied to another org or
key id does not unwrap.

* :class:`LocalFileKeyProvider` (default, ``KEY_PROVIDER=local``): the KEK is base64 of 32 bytes in
  ``MASTER_KEY_FILE`` (preferred: a file readable only by the service user / a Docker secret) or
  ``MASTER_KEY``; ``MASTER_OLD_KEYS=id:base64,...`` unwrap only (KEK rotation).
* :class:`RemoteKmsKeyProvider` is a documented STUB for AWS KMS, GCP KMS and Vault transit; no
  cloud SDK is a dependency. An implementation maps 1:1:

  - AWS KMS: ``wrap`` = ``Encrypt(KeyId=MASTER_KEY_ID, Plaintext=dek,
    EncryptionContext={"ukos": b64(context)})``; ``unwrap`` = ``Decrypt`` with the same context
    (the returned ``KeyId`` is the stored kek id). Rotation = KMS automatic key rotation plus
    ``rotate-keys.sh kek`` to re-wrap under a new CMK.
  - GCP KMS: ``projects/../cryptoKeys/<k>:encrypt`` / ``:decrypt`` with
    ``additionalAuthenticatedData=context``; the crypto key version is the kek id.
  - Vault transit: ``POST transit/encrypt/<k>`` with ``context=b64(context)`` (derived keys) /
    ``transit/decrypt/<k>``; ``vault:vN:`` prefix is the kek id; ``transit/rewrap`` for rotation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings
from app.core.crypto.errors import (
    KEY_BYTES,
    NONCE_BYTES,
    CryptoNotConfigured,
    DecryptionFailed,
    decode_key,
    parse_old_keys,
)

WRAP_PREFIX = b"ukos-kek-v1|"


@runtime_checkable
class KeyProvider(Protocol):
    @property
    def active_key_id(self) -> str: ...

    def wrap(self, dek: bytes, *, context: bytes) -> tuple[str, bytes]:
        """Return ``(kek_id, wrapped)``; ``wrapped`` is opaque."""
        ...

    def unwrap(self, wrapped: bytes, kek_id: str, *, context: bytes) -> bytes: ...

    def knows(self, kek_id: str) -> bool: ...


class LocalFileKeyProvider:
    """AES-256-GCM key wrap with a local KEK; ``wrapped = nonce(12) || ciphertext+tag``."""

    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise CryptoNotConfigured("the active master key id has no key")
        if any(len(k) != KEY_BYTES for k in keys.values()):
            raise CryptoNotConfigured("master keys must be 32 bytes")
        self._keys = {kid: AESGCM(k) for kid, k in keys.items()}
        self._active = active_key_id

    def __repr__(self) -> str:
        return f"LocalFileKeyProvider(active={self._active!r}, ids={sorted(self._keys)!r})"

    @property
    def active_key_id(self) -> str:
        return self._active

    def knows(self, kek_id: str) -> bool:
        return kek_id in self._keys

    def wrap(self, dek: bytes, *, context: bytes) -> tuple[str, bytes]:
        if len(dek) != KEY_BYTES:
            raise CryptoNotConfigured("data keys must be 32 bytes")
        nonce = os.urandom(NONCE_BYTES)
        body = self._keys[self._active].encrypt(nonce, dek, WRAP_PREFIX + context)
        return self._active, nonce + body

    def unwrap(self, wrapped: bytes, kek_id: str, *, context: bytes) -> bytes:
        aead = self._keys.get(kek_id)
        if aead is None or len(wrapped) <= NONCE_BYTES:
            raise DecryptionFailed("cannot unwrap the data key")
        try:
            dek = aead.decrypt(
                bytes(wrapped[:NONCE_BYTES]), bytes(wrapped[NONCE_BYTES:]), WRAP_PREFIX + context
            )
        except InvalidTag:
            raise DecryptionFailed("cannot unwrap the data key") from None
        if len(dek) != KEY_BYTES:
            raise DecryptionFailed("cannot unwrap the data key")
        return dek


class RemoteKmsKeyProvider:
    """STUB adapter for an external KMS (see the module docstring for the exact API mapping)."""

    def __init__(self, kind: str, key_id: str) -> None:
        self.kind = kind
        self._active = key_id

    def __repr__(self) -> str:
        return f"RemoteKmsKeyProvider(kind={self.kind!r}, active={self._active!r})"

    @property
    def active_key_id(self) -> str:
        return self._active

    def knows(self, kek_id: str) -> bool:
        return kek_id == self._active

    def wrap(self, dek: bytes, *, context: bytes) -> tuple[str, bytes]:
        raise CryptoNotConfigured(f"KEY_PROVIDER={self.kind} is not implemented (stub)")

    def unwrap(self, wrapped: bytes, kek_id: str, *, context: bytes) -> bytes:
        raise CryptoNotConfigured(f"KEY_PROVIDER={self.kind} is not implemented (stub)")


def envelope_enabled(settings: Settings) -> bool:
    return settings.MASTER_KEY_FILE is not None or settings.MASTER_KEY is not None


def provider_from_settings(settings: Settings) -> KeyProvider:
    """Build the configured provider or raise :class:`CryptoNotConfigured`."""
    if settings.KEY_PROVIDER != "local":
        return RemoteKmsKeyProvider(settings.KEY_PROVIDER, settings.MASTER_KEY_ID)
    if settings.MASTER_KEY_FILE is not None:
        try:
            encoded = Path(settings.MASTER_KEY_FILE).read_text(encoding="utf-8")
        except OSError:
            raise CryptoNotConfigured("MASTER_KEY_FILE cannot be read") from None
    elif settings.MASTER_KEY is not None:
        encoded = settings.MASTER_KEY.get_secret_value()
    else:
        raise CryptoNotConfigured("no master key is configured")
    keys = {settings.MASTER_KEY_ID: decode_key(encoded, what="master key")}
    if settings.MASTER_OLD_KEYS is not None:
        for kid, key in parse_old_keys(
            settings.MASTER_OLD_KEYS.get_secret_value(), what="MASTER_OLD_KEYS"
        ).items():
            keys.setdefault(kid, key)
    return LocalFileKeyProvider(keys, settings.MASTER_KEY_ID)
