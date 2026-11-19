"""Secret encryption for connector credentials (M06.01.03; contract m06 section 1, rule 2).

AES-256-GCM. A stored secret is ``nonce(12) || ciphertext+tag``; the ``key_id`` of the key that
sealed it is stored beside it, so a key can be rotated: new writes use the active key, old rows keep
decrypting through ``CONNECTOR_ENCRYPTION_OLD_KEYS`` until they are re-sealed. The caller passes
``aad`` (e.g. the account id) so a ciphertext copied to another row does not open.

Secrets are plain ``str`` only inside :func:`TokenCipher.open`'s return value, which callers hand to
the HTTP client and never log, return or audit. The cipher's ``repr`` shows nothing about keys.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

NONCE_BYTES = 12
KEY_BYTES = 32


class CryptoError(Exception):
    """Base class; messages never contain key or plaintext material."""


class CryptoNotConfigured(CryptoError):
    """``CONNECTOR_ENCRYPTION_KEY`` is missing or malformed but a secret must be stored/read."""


class DecryptionFailed(CryptoError):
    """Wrong key, unknown ``key_id``, a different ``aad`` or tampered bytes."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    key_id: str


def _decode_key(value: str) -> bytes:
    try:
        raw = base64.b64decode(value.strip() + "=" * (-len(value.strip()) % 4), validate=False)
    except (binascii.Error, ValueError):
        raise CryptoNotConfigured("connector encryption key is not valid base64") from None
    if value.strip().startswith("change-me") or len(raw) != KEY_BYTES:
        raise CryptoNotConfigured("connector encryption key must be base64 of 32 random bytes")
    return raw


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
