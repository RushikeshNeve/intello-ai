# M14 contracts: AI firewall and security

## M14.08
- Envelope encryption (`app/core/crypto`): `KeyProvider` (KEK) wraps per-org DEKs in `tenant_keys` (0026: id, org_id, kek_id, wrapped_key, state active|retired, FORCE RLS; app/worker SELECT+INSERT only; one active per org). `LocalFileKeyProvider` (`MASTER_KEY_FILE`/`MASTER_KEY`, `MASTER_KEY_ID`, `MASTER_OLD_KEYS`); AWS/GCP KMS and Vault are a documented stub (`KEY_PROVIDER`). Unset master key = M06 single-key behaviour unchanged.
- Sealed value: AES-256-GCM, key id `dek:<uuid>`, aad = `ukos-dek-v1|org_id|row id`; wrap aad = `ukos-kek-v1|org_id|dek_id`.
- Connector tokens + webhook secrets use `connectors.crypto.secret_cipher(session, org, settings)`: dual read of legacy `k1` rows; the sync re-seals a legacy token lazily (worker gains UPDATE(token_ciphertext, key_id)); `rotate-keys.sh reencrypt` migrates the rest.
- `scripts/ops/rotate-keys.sh kek|dek --org|reencrypt|verify|shred --org --yes` (dry run + verify around each change; batched, SKIP LOCKED, resumable, idempotent). Shred = delete the org's DEKs; legacy rows are not covered until re-encrypted.
- Backups: `BACKUP_ENCRYPTION_KEY_FILE` -> `payload.tar.enc` (openssl aes-256-cbc pbkdf2 200k; manifest `encrypted: true`, `key_id` = label or key fingerprint, payload sha256); restore.sh decrypts to a temp dir, then the normal manifest verify (plaintext hashes = integrity).
- Not done: memory/capture text columns stay plaintext (FTS/search and the evidence pipeline read them; encrypting them would break retrieval for little gain over disk encryption). Compose files do not yet pass `MASTER_*` to api/worker (mount the key file as a secret; follow-up).
