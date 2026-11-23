"""Per-org data keys (``tenant_keys``) and the cipher that seals secrets with them.

The ``db`` argument is a SQLAlchemy ``Session`` or ``Connection``. Under the app/worker roles the
FORCE RLS policy of ``tenant_keys`` confines every read to ``app.current_org_id``; every query here
ALSO filters by ``org_id`` so the admin (cross-org operator) path is equally confined.

Ciphertext format of a DEK-sealed secret: ``nonce(12) || ciphertext+tag`` (AES-256-GCM), key id
``dek:<tenant_keys.id>``, associated data ``"ukos-dek-v1|" || org_id || caller aad``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from app.core.crypto.errors import KEY_BYTES, NONCE_BYTES, DecryptionFailed, Sealed
from app.core.crypto.providers import KeyProvider

DEK_PREFIX = "dek:"
SEAL_PREFIX = b"ukos-dek-v1|"
STATE_ACTIVE = "active"
STATE_RETIRED = "retired"


class LegacyOpener(Protocol):
    def open(self, ciphertext: bytes, key_id: str, *, aad: bytes = b"") -> str: ...


@dataclass(frozen=True)
class DataKey:
    id: uuid.UUID
    org_id: uuid.UUID
    raw: bytes = field(repr=False)

    @property
    def key_id(self) -> str:
        return f"{DEK_PREFIX}{self.id}"


def wrap_context(org_id: uuid.UUID, dek_id: uuid.UUID) -> bytes:
    return org_id.bytes + dek_id.bytes


def is_dek_key_id(key_id: str | None) -> bool:
    return bool(key_id) and str(key_id).startswith(DEK_PREFIX)


def _parse_dek_id(key_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(key_id[len(DEK_PREFIX) :])
    except ValueError:
        return None


def _unwrap(provider: KeyProvider, org_id: uuid.UUID, row: Any) -> DataKey:
    raw = provider.unwrap(bytes(row.wrapped_key), row.kek_id, context=wrap_context(org_id, row.id))
    return DataKey(row.id, org_id, raw)


def _insert_dek(db: Any, org_id: uuid.UUID, provider: KeyProvider, *, conflict_ok: bool) -> None:
    dek_id = uuid.uuid4()
    kek_id, wrapped = provider.wrap(os.urandom(KEY_BYTES), context=wrap_context(org_id, dek_id))
    sql = (
        "INSERT INTO tenant_keys (id, org_id, kek_id, wrapped_key, state)"
        " VALUES (:i, :o, :k, :w, 'active')"
    )
    if conflict_ok:
        sql += " ON CONFLICT (org_id) WHERE state = 'active' DO NOTHING"
    db.execute(text(sql), {"i": dek_id, "o": org_id, "k": kek_id, "w": wrapped})


def load_active_dek(
    db: Any, org_id: uuid.UUID, provider: KeyProvider, *, create: bool = True
) -> DataKey | None:
    """The org's active DEK; created on first use (race-safe: one active per org, unique index)."""
    sql = text(
        "SELECT id, kek_id, wrapped_key FROM tenant_keys WHERE org_id = :o AND state = 'active'"
    )
    row = db.execute(sql, {"o": org_id}).one_or_none()
    if row is None and create:
        _insert_dek(db, org_id, provider, conflict_ok=True)
        row = db.execute(sql, {"o": org_id}).one_or_none()
    return None if row is None else _unwrap(provider, org_id, row)


def load_dek(
    db: Any, org_id: uuid.UUID, dek_id: uuid.UUID, provider: KeyProvider
) -> DataKey | None:
    row = db.execute(
        text("SELECT id, kek_id, wrapped_key FROM tenant_keys WHERE org_id = :o AND id = :i"),
        {"o": org_id, "i": dek_id},
    ).one_or_none()
    return None if row is None else _unwrap(provider, org_id, row)


def issue_new_dek(db: Any, org_id: uuid.UUID, provider: KeyProvider) -> uuid.UUID:
    """Retire the org's active DEK (kept for reading) and make a new one active (operator path)."""
    db.execute(
        text(
            "UPDATE tenant_keys SET state = 'retired', retired_at = now()"
            " WHERE org_id = :o AND state = 'active'"
        ),
        {"o": org_id},
    )
    _insert_dek(db, org_id, provider, conflict_ok=False)
    return uuid.UUID(
        str(
            db.execute(
                text("SELECT id FROM tenant_keys WHERE org_id = :o AND state = 'active'"),
                {"o": org_id},
            ).scalar_one()
        )
    )


def shred_org_keys(db: Any, org_id: uuid.UUID) -> int:
    """Crypto-shred: delete every DEK of the org. Its ``dek:`` ciphertexts can never open again."""
    result = db.execute(text("DELETE FROM tenant_keys WHERE org_id = :o"), {"o": org_id})
    return int(result.rowcount or 0)


class TenantCipher:
    """Seal with the org's active DEK; open ``dek:`` rows with the org's DEKs and anything else
    with the legacy single-key cipher (dual read during the migration)."""

    def __init__(
        self,
        db: Any,
        org_id: uuid.UUID,
        provider: KeyProvider,
        legacy: LegacyOpener | None = None,
    ) -> None:
        self._db = db
        self._org = org_id
        self._provider = provider
        self._legacy = legacy
        self._active: DataKey | None = None
        self._cache: dict[uuid.UUID, DataKey | None] = {}

    def __repr__(self) -> str:
        return f"TenantCipher(org={self._org}, provider={self._provider!r})"

    def _active_key(self) -> DataKey:
        if self._active is None:
            dek = load_active_dek(self._db, self._org, self._provider)
            if dek is None:  # pragma: no cover - insert then select in one transaction
                raise DecryptionFailed("no data key for this organization")
            self._active = dek
            self._cache[dek.id] = dek
        return self._active

    @property
    def active_key_id(self) -> str:
        return self._active_key().key_id

    def is_current(self, key_id: str | None) -> bool:
        return key_id == self.active_key_id

    def seal(self, plaintext: str, *, aad: bytes = b"") -> Sealed:
        dek = self._active_key()
        nonce = os.urandom(NONCE_BYTES)
        body = AESGCM(dek.raw).encrypt(
            nonce, plaintext.encode("utf-8"), SEAL_PREFIX + self._org.bytes + aad
        )
        return Sealed(nonce + body, dek.key_id)

    def open(self, ciphertext: bytes, key_id: str, *, aad: bytes = b"") -> str:
        if not is_dek_key_id(key_id):
            if self._legacy is None:
                raise DecryptionFailed("cannot decrypt the stored secret")
            return self._legacy.open(ciphertext, key_id, aad=aad)
        dek_id = _parse_dek_id(key_id)
        if dek_id is None or len(ciphertext) <= NONCE_BYTES:
            raise DecryptionFailed("cannot decrypt the stored secret")
        if dek_id not in self._cache:
            self._cache[dek_id] = load_dek(self._db, self._org, dek_id, self._provider)
        dek = self._cache[dek_id]
        if dek is None:  # another org's key (RLS / org filter) or a shredded key
            raise DecryptionFailed("cannot decrypt the stored secret")
        try:
            plain = AESGCM(dek.raw).decrypt(
                bytes(ciphertext[:NONCE_BYTES]),
                bytes(ciphertext[NONCE_BYTES:]),
                SEAL_PREFIX + self._org.bytes + aad,
            )
        except InvalidTag:
            raise DecryptionFailed("cannot decrypt the stored secret") from None
        return plain.decode("utf-8")
