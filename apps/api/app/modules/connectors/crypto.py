"""Secret encryption for connector credentials (M06.01.03; contract m06 section 1, rule 2).

AES-256-GCM. A stored secret is ``nonce(12) || ciphertext+tag``; the ``key_id`` of the key that
sealed it is stored beside it, so a key can be rotated: new writes use the active key, old rows keep
decrypting through ``CONNECTOR_ENCRYPTION_OLD_KEYS`` until they are re-sealed. The caller passes
``aad`` (e.g. the account id) so a ciphertext copied to another row does not open.

Secrets are plain ``str`` only inside :func:`TokenCipher.open`'s return value, which callers hand to
the HTTP client and never log, return or audit. The cipher's ``repr`` shows nothing about keys.

M14.08: :func:`secret_cipher` layers per-org envelope encryption (``app.core.crypto``) on top;
``TokenCipher`` stays as the legacy reader of rows sealed before the master key was configured.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings
from app.core.crypto.envelope import TenantCipher, is_dek_key_id
from app.core.crypto.errors import (
    KEY_BYTES,
    NONCE_BYTES,
    CryptoError,
    CryptoNotConfigured,
    DecryptionFailed,
    Sealed,
    decode_key,
)
from app.core.crypto.providers import envelope_enabled, provider_from_settings

__all__ = [
    "CryptoError",
    "CryptoNotConfigured",
    "DecryptionFailed",
    "Sealed",
    "SecretCipher",
    "TenantCipher",
    "TokenCipher",
    "cipher_from_settings",
    "is_dek_key_id",
    "secret_cipher",
]


def _decode_key(value: str) -> bytes:
    return decode_key(value, what="connector encryption key")


class TokenCipher:
    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise CryptoNotConfigured("the active connector key id has no key")
        if any(len(k) != KEY_BYTES for k in keys.values()):
            raise CryptoNotConfigured("connector keys must be 32 bytes")
        self._keys = {kid: AESGCM(k) for kid, k in keys.items()}
        self._active = active_key_id

    def __repr__(self) -> str:
        return f"TokenCipher(active={self._active!r})"

    @property
    def active_key_id(self) -> str:
        return self._active

    def seal(self, plaintext: str, *, aad: bytes = b"") -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        body = self._keys[self._active].encrypt(nonce, plaintext.encode("utf-8"), aad)
        return Sealed(nonce + body, self._active)

    def open(self, ciphertext: bytes, key_id: str, *, aad: bytes = b"") -> str:
        aead = self._keys.get(key_id)
        if aead is None or len(ciphertext) <= NONCE_BYTES:
            raise DecryptionFailed("cannot decrypt the stored secret")
        try:
            plain = aead.decrypt(
                bytes(ciphertext[:NONCE_BYTES]), bytes(ciphertext[NONCE_BYTES:]), aad
            )
        except InvalidTag:
            raise DecryptionFailed("cannot decrypt the stored secret") from None
        return plain.decode("utf-8")


def cipher_from_settings(settings: Settings) -> TokenCipher:
    """Build the cipher or raise :class:`CryptoNotConfigured` (callers answer a generic error)."""
    if settings.CONNECTOR_ENCRYPTION_KEY is None:
        raise CryptoNotConfigured("CONNECTOR_ENCRYPTION_KEY is not set")
    keys = {
        settings.CONNECTOR_ENCRYPTION_KEY_ID: _decode_key(
            settings.CONNECTOR_ENCRYPTION_KEY.get_secret_value()
        )
    }
    if settings.CONNECTOR_ENCRYPTION_OLD_KEYS is not None:
        for part in settings.CONNECTOR_ENCRYPTION_OLD_KEYS.get_secret_value().split(","):
            if not part.strip():
                continue
            key_id, sep, value = part.strip().partition(":")
            if not sep or not key_id:
                raise CryptoNotConfigured("CONNECTOR_ENCRYPTION_OLD_KEYS must be id:base64,...")
            keys.setdefault(key_id, _decode_key(value))
    return TokenCipher(keys, settings.CONNECTOR_ENCRYPTION_KEY_ID)


class SecretCipher(Protocol):
    def seal(self, plaintext: str, *, aad: bytes = b"") -> Sealed: ...

    def open(self, ciphertext: bytes, key_id: str, *, aad: bytes = b"") -> str: ...


def secret_cipher(db: Any, org_id: uuid.UUID, settings: Settings) -> SecretCipher:
    """The cipher for one org's connector secrets (M14.08).

    With a master key configured: the org's data key seals new values (``dek:<id>``) and the single
    connector key, when still configured, keeps opening legacy rows (dual read). Without one: the
    M06 single-key :class:`TokenCipher` (unchanged behaviour). ``db`` is the org's tenant session.
    Raises :class:`CryptoNotConfigured` when neither is configured.
    """
    if not envelope_enabled(settings):
        return cipher_from_settings(settings)
    legacy = (
        cipher_from_settings(settings) if settings.CONNECTOR_ENCRYPTION_KEY is not None else None
    )
    return TenantCipher(db, org_id, provider_from_settings(settings), legacy)
