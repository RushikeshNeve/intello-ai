"""Errors and value types shared by every cipher (messages never contain key or plaintext bytes)."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

KEY_BYTES = 32
NONCE_BYTES = 12


class CryptoError(Exception):
    """Base class; messages never contain key or plaintext material."""


class CryptoNotConfigured(CryptoError):
    """A key is missing or malformed but a secret must be stored/read."""


class DecryptionFailed(CryptoError):
    """Wrong key, unknown key id, a different ``aad``, a shredded data key or tampered bytes."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    key_id: str


def decode_key(value: str, *, what: str = "key") -> bytes:
    """base64 of exactly 32 bytes, else :class:`CryptoNotConfigured` (the value is never echoed)."""
    stripped = value.strip()
    try:
        raw = base64.b64decode(stripped + "=" * (-len(stripped) % 4), validate=False)
    except (binascii.Error, ValueError):
        raise CryptoNotConfigured(f"{what} is not valid base64") from None
    if stripped.startswith("change-me") or len(raw) != KEY_BYTES:
        raise CryptoNotConfigured(f"{what} must be base64 of 32 random bytes")
    return raw


def parse_old_keys(value: str, *, what: str) -> dict[str, bytes]:
    """``id:base64,id:base64`` -> ``{id: key}``."""
    keys: dict[str, bytes] = {}
    for part in value.split(","):
        if not part.strip():
            continue
        key_id, sep, encoded = part.strip().partition(":")
        if not sep or not key_id:
            raise CryptoNotConfigured(f"{what} must be id:base64,...")
        keys.setdefault(key_id, decode_key(encoded, what=what))
    return keys
