"""Operator command: re-seal connector secrets under the active encryption key (M08.01.04).

``python -m app.modules.connectors.rotate [--dry-run]``

After changing ``CONNECTOR_ENCRYPTION_KEY`` (new ``CONNECTOR_ENCRYPTION_KEY_ID``) and listing the
previous key in ``CONNECTOR_ENCRYPTION_OLD_KEYS``, rows sealed under an old key keep working. This
command re-seals every stored token and webhook secret still under another key id, so the old key
can then be removed. Like the backfill tool it connects with the migration (admin) URL for the one
cross-org pass; it never prints or logs a secret, only counts. A row that cannot be opened is
counted as ``failed`` and left untouched (the old key is still needed for it). Idempotent.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.core.config import Settings, get_settings
from app.core.db import create_db_engine
from app.modules.connectors import crypto


@dataclass(frozen=True)
class RotationReport:
    tokens: int
    webhook_secrets: int
    failed: int


def rotate_secrets(
    conn: Connection, cipher: crypto.TokenCipher, *, dry_run: bool = False
) -> RotationReport:
    """Re-seal every secret not sealed under the active key. Runs in the caller's transaction."""
    active = cipher.active_key_id
    tokens = hooks = failed = 0
    accounts = conn.execute(
        text(
            "SELECT id, token_ciphertext, key_id FROM connector_accounts"
            " WHERE token_ciphertext IS NOT NULL AND key_id <> :k FOR UPDATE"
        ),
        {"k": active},
    ).all()
    for row in accounts:
        try:
            plain = cipher.open(bytes(row.token_ciphertext), row.key_id, aad=row.id.bytes)
        except crypto.DecryptionFailed:
            failed += 1
            continue
        tokens += 1
        if not dry_run:
            sealed = cipher.seal(plain, aad=row.id.bytes)
            conn.execute(
                text(
                    "UPDATE connector_accounts SET token_ciphertext = :c, key_id = :k WHERE id = :i"
                ),
                {"c": sealed.ciphertext, "k": sealed.key_id, "i": row.id},
            )
    sources = conn.execute(
        text(
            "SELECT id, webhook_secret_ciphertext, webhook_key_id FROM sources"
            " WHERE webhook_secret_ciphertext IS NOT NULL AND webhook_key_id <> :k FOR UPDATE"
        ),
        {"k": active},
    ).all()
    for row in sources:
        try:
            plain = cipher.open(
                bytes(row.webhook_secret_ciphertext), row.webhook_key_id, aad=row.id.bytes
            )
        except crypto.DecryptionFailed:
            failed += 1
            continue
        hooks += 1
        if not dry_run:
            sealed = cipher.seal(plain, aad=row.id.bytes)
            conn.execute(
                text(
                    "UPDATE sources SET webhook_secret_ciphertext = :c, webhook_key_id = :k"
                    " WHERE id = :i"
                ),
                {"c": sealed.ciphertext, "k": sealed.key_id, "i": row.id},
            )
    return RotationReport(tokens, hooks, failed)


def run(settings: Settings, *, dry_run: bool) -> RotationReport:
    cipher = crypto.cipher_from_settings(settings)
    engine = create_db_engine(settings.sqlalchemy_migration_url, pool_size=1, max_overflow=0)
    try:
        with engine.begin() as conn:
            return rotate_secrets(conn, cipher, dry_run=dry_run)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.modules.connectors.rotate")
    parser.add_argument("--dry-run", action="store_true", help="count rows, change nothing")
    args = parser.parse_args(argv)
    try:
        report = run(get_settings(), dry_run=args.dry_run)
    except crypto.CryptoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"tokens={report.tokens} webhook_secrets={report.webhook_secrets} "
        f"failed={report.failed} dry_run={args.dry_run}"
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
