# ruff: noqa: S608  (table/column names come only from the fixed ENCRYPTED_COLUMNS registry)
"""Operator commands for keys (M14.08); wrapped by ``scripts/ops/rotate-keys.sh``.

``python -m app.core.crypto.rotate <command> [--org UUID] [--dry-run] [--batch N] [--yes]``

* ``kek``        re-wrap every DEK still wrapped by an old master key under the active one
                 (old key listed in ``MASTER_OLD_KEYS``). Batched, idempotent.
* ``new-dek``    retire the org's active DEK and issue a new one (old one keeps opening rows).
* ``reencrypt``  re-seal every encrypted column of one org (``--org``) or all orgs under its active
                 DEK: legacy single-key rows (the connector key migration) and rows of retired
                 DEKs. One transaction per batch with ``FOR UPDATE SKIP LOCKED`` (online; a rerun
                 resumes where it stopped), idempotent.
* ``verify``     every DEK unwraps and every encrypted value opens; counts legacy/stale rows.
* ``shred``      crypto-shred: delete all DEKs of ``--org`` (needs ``--yes``). Its ``dek:`` values
                 become unrecoverable; legacy rows are NOT covered (run ``reencrypt`` first).

Connects with the migration (admin) URL for the cross-org pass, like the M08 rotation tool.
Prints counts and ids only; never a key or a secret. Exit 1 when a row failed, 2 on config errors.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from app.core.config import Settings, get_settings
from app.core.crypto.envelope import (
    LegacyOpener,
    TenantCipher,
    is_dek_key_id,
    issue_new_dek,
    load_active_dek,
    shred_org_keys,
    wrap_context,
)
from app.core.crypto.errors import CryptoError, DecryptionFailed
from app.core.crypto.providers import KeyProvider, provider_from_settings


@dataclass(frozen=True)
class EncryptedColumn:
    table: str
    ciphertext: str
    key_id: str  # aad = the row id (bytes), as sealed by the connectors module


# Every envelope-encrypted column. Memory/capture text is deliberately NOT here (m14-contracts).
ENCRYPTED_COLUMNS: tuple[EncryptedColumn, ...] = (
    EncryptedColumn("connector_accounts", "token_ciphertext", "key_id"),
    EncryptedColumn("sources", "webhook_secret_ciphertext", "webhook_key_id"),
)
ZERO = uuid.UUID(int=0)


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    @property
    def failed(self) -> int:
        return self.counts.get("failed", 0)

    def line(self) -> str:
        return " ".join(f"{k}={v}" for k, v in sorted(self.counts.items())) or "nothing=0"


def rotate_kek(engine: Engine, provider: KeyProvider, *, dry_run: bool, batch: int) -> Report:
    report = Report()
    active, last = provider.active_key_id, ZERO
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, org_id, kek_id, wrapped_key FROM tenant_keys"
                    " WHERE kek_id <> :k AND id > :l ORDER BY id LIMIT :b FOR UPDATE SKIP LOCKED"
                ),
                {"k": active, "l": last, "b": batch},
            ).all()
            if not rows:
                return report
            for row in rows:
                ctx = wrap_context(row.org_id, row.id)
                try:
                    dek = provider.unwrap(bytes(row.wrapped_key), row.kek_id, context=ctx)
                except CryptoError:
                    report.add("failed")
                    continue
                report.add("rewrapped")
                if not dry_run:
                    kek_id, wrapped = provider.wrap(dek, context=ctx)
                    conn.execute(
                        text("UPDATE tenant_keys SET kek_id = :k, wrapped_key = :w WHERE id = :i"),
                        {"k": kek_id, "w": wrapped, "i": row.id},
                    )
            last = rows[-1].id
            report.add("batches")


def _orgs(engine: Engine, col: EncryptedColumn, org: uuid.UUID | None) -> list[uuid.UUID]:
    if org is not None:
        return [org]
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(f"SELECT DISTINCT org_id FROM {col.table} WHERE {col.ciphertext} IS NOT NULL")
            ).scalars()
        )


def reencrypt(
    engine: Engine,
    provider: KeyProvider,
    legacy: LegacyOpener | None,
    *,
    org: uuid.UUID | None = None,
    dry_run: bool = False,
    batch: int = 200,
) -> Report:
    report = Report()
    for col in ENCRYPTED_COLUMNS:
        select_sql = text(
            f"SELECT id, {col.ciphertext} AS ct, {col.key_id} AS kid FROM {col.table}"
            f" WHERE org_id = :o AND {col.ciphertext} IS NOT NULL"
            f" AND {col.key_id} IS DISTINCT FROM :a AND id > :l"
            " ORDER BY id LIMIT :b FOR UPDATE SKIP LOCKED"
        )
        update_sql = text(
            f"UPDATE {col.table} SET {col.ciphertext} = :c, {col.key_id} = :k WHERE id = :i"
        )
        for org_id in _orgs(engine, col, org):
            last = ZERO
            while True:
                with engine.begin() as conn:
                    cipher = TenantCipher(conn, org_id, provider, legacy)
                    if dry_run:
                        current = load_active_dek(conn, org_id, provider, create=False)
                        active = current.key_id if current else ""
                    else:
                        active = cipher.active_key_id
                    rows = conn.execute(
                        select_sql, {"o": org_id, "a": active, "l": last, "b": batch}
                    ).all()
                    if not rows:
                        break
                    for row in rows:
                        try:
                            plain = cipher.open(bytes(row.ct), row.kid or "", aad=row.id.bytes)
                        except CryptoError:
                            report.add("failed")
                            continue
                        report.add("resealed")
                        if not dry_run:
                            sealed = cipher.seal(plain, aad=row.id.bytes)
                            conn.execute(
                                update_sql,
                                {"c": sealed.ciphertext, "k": sealed.key_id, "i": row.id},
                            )
                    last = rows[-1].id
                    report.add("batches")
    return report


def verify(
    engine: Engine,
    provider: KeyProvider,
    legacy: LegacyOpener | None,
    *,
    org: uuid.UUID | None = None,
) -> Report:
    report = Report()
    where, params = ("WHERE org_id = :o", {"o": org}) if org else ("", {})
    with engine.connect() as conn:
        for row in conn.execute(
            text(f"SELECT id, org_id, kek_id, wrapped_key FROM tenant_keys {where}"), params
        ):
            try:
                provider.unwrap(
                    bytes(row.wrapped_key), row.kek_id, context=wrap_context(row.org_id, row.id)
                )
                report.add("keys_ok")
                if row.kek_id != provider.active_key_id:
                    report.add("keys_old_kek")
            except CryptoError:
                report.add("failed")
        for col in ENCRYPTED_COLUMNS:
            rows = conn.execute(
                text(
                    f"SELECT id, org_id, {col.ciphertext} AS ct, {col.key_id} AS kid"
                    f" FROM {col.table} WHERE {col.ciphertext} IS NOT NULL"
                    + (" AND org_id = :o" if org else "")
                ),
                params,
            ).all()
            ciphers: dict[uuid.UUID, TenantCipher] = {}
            for row in rows:
                cipher = ciphers.setdefault(
                    row.org_id, TenantCipher(conn, row.org_id, provider, legacy)
                )
                try:
                    cipher.open(bytes(row.ct), row.kid or "", aad=row.id.bytes)
                    report.add("values_ok")
                except CryptoError:
                    report.add("failed")
                if not is_dek_key_id(row.kid):
                    report.add("values_legacy")
    return report


def _legacy(settings: Settings) -> LegacyOpener | None:
    if settings.CONNECTOR_ENCRYPTION_KEY is None:
        return None
    from app.modules.connectors import crypto as connector_crypto  # operator path only

    return connector_crypto.cipher_from_settings(settings)


def run(settings: Settings, args: Any) -> Report:
    from app.core.db import create_db_engine

    provider = provider_from_settings(settings)
    org = uuid.UUID(args.org) if args.org else None
    engine = create_db_engine(settings.sqlalchemy_migration_url, pool_size=1, max_overflow=0)
    try:
        if args.command == "kek":
            return rotate_kek(engine, provider, dry_run=args.dry_run, batch=args.batch)
        if args.command == "verify":
            return verify(engine, provider, _legacy(settings), org=org)
        if args.command == "reencrypt":
            return reencrypt(
                engine, provider, _legacy(settings), org=org, dry_run=args.dry_run, batch=args.batch
            )
        if org is None:
            raise SystemExit(f"{args.command} needs --org")
        report = Report()
        if args.command == "new-dek":
            if not args.dry_run:
                with engine.begin() as conn:
                    issue_new_dek(conn, org, provider)
            report.add("issued")
            return report
        # shred
        if not args.yes:
            raise SystemExit("shred is irreversible: pass --yes")
        with engine.begin() as conn:
            report.add("keys_deleted", 0 if args.dry_run else shred_org_keys(conn, org))
            for col in ENCRYPTED_COLUMNS:
                legacy_rows = conn.execute(
                    text(
                        f"SELECT count(*) FROM {col.table} WHERE org_id = :o"
                        f" AND {col.ciphertext} IS NOT NULL AND {col.key_id} NOT LIKE 'dek:%'"
                    ),
                    {"o": org},
                ).scalar_one()
                report.add("legacy_rows_not_shredded", int(legacy_rows))
        return report
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.core.crypto.rotate")
    parser.add_argument("command", choices=("kek", "new-dek", "reencrypt", "verify", "shred"))
    parser.add_argument("--org", help="organization id")
    parser.add_argument("--dry-run", action="store_true", help="count, change nothing")
    parser.add_argument("--batch", type=int, default=200)
    parser.add_argument("--yes", action="store_true", help="confirm shred")
    args = parser.parse_args(argv)
    try:
        report = run(get_settings(), args)
    except (CryptoError, DecryptionFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{args.command}: {report.line()} dry_run={args.dry_run}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
