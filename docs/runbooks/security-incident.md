# Runbook: security incidents

Local-first deployment, single operator. Principle: **contain first, then investigate, then rotate, then record.**
Legend: **[V]** verified in the project, **[U]** UNTESTED (written from the design; rehearse before relying on it).
Commands run in Git Bash from the repo root (`export MSYS_NO_PATHCONV=1`); `$P` is your compose project (`ukos`).

Useful facts: `audit_events` (PostgreSQL, append-only, redacted: no tokens or document text) records who did what
(`event_type`, `actor_user_id`, `org_id`, `target_type/id`, `outcome` success|denied|error, `occurred_at`). Query it as the admin role:
```bash
docker compose exec -T postgres psql -U ukos_admin -d ukos -c \
 "select occurred_at,event_type,actor_user_id,org_id,target_type,outcome from audit_events where occurred_at > now() - interval '24 hours' order by occurred_at desc limit 50"   # [U]
```
Application logs: `docker compose logs --no-color --since 24h api web worker dispatcher`. They are designed to hold no secrets (M03/M08 log scans).

## 1. Credential or secret leak (env file, token, key pasted or committed)
1. **Contain:** if it was committed or posted, treat it as public. Remove it from the place (history rewrite is secondary; rotation is mandatory).
2. **Identify what it unlocks:** `.env` -> everything below; `OPENAI_API_KEY` -> hosted provider; connector token -> that external account;
   `OIDC_CLIENT_SECRET`/`WEB_SESSION_SECRET` -> sign-in; `CONNECTOR_ENCRYPTION_KEY` -> stored connector tokens.
3. **Rotate** (all [U]):
   - Hosted provider key / connector token: revoke at the provider first (their console), then update `.env` / reconnect the source.
   - `WEB_SESSION_SECRET`, `OIDC_CLIENT_SECRET`: new random value in `.env`, `docker compose up -d dex web api`; all sessions end.
   - `CONNECTOR_ENCRYPTION_KEY`: new key + new `CONNECTOR_ENCRYPTION_KEY_ID`, old key in `CONNECTOR_ENCRYPTION_OLD_KEYS=id:base64` (see operations.md), then re-save connector tokens and drop the old key.
   - Master key (`MASTER_KEY_FILE`, M14.08): new key file + `MASTER_KEY_ID`, old one in `MASTER_OLD_KEYS=id:base64`, `bash scripts/ops/rotate-keys.sh kek`, then drop the old key. One org's data key: `rotate-keys.sh dek --org <uuid>`. Org deletion request: `rotate-keys.sh shred --org <uuid> --yes` (crypto-shred; irreversible).
   - Database/Redis/RushDB/S3 secrets: change inside the service first (`ALTER ROLE ukos_app PASSWORD '...'`, `redis-cli CONFIG SET requirepass`, RushDB/S3 admin), then `.env`, then `docker compose up -d`. Never wipe volumes just to rotate.
4. **Check for abuse:** audit query above for unexpected `outcome=success` events by unknown actors; provider/connector consoles for usage.
5. **Record:** date, what leaked, what was rotated, in `docs/execution/BLOCKERS.md` or your own log.
Prevention already in place: CI secret scan (`scripts/ci/secret_scan.py`), `.env` git-ignored, no secret in compose files, logs scanned by the gates.

## 2. Suspected tenant-isolation breach (a user saw another organization's data)
1. **Contain:** stop the web and api so no more reads happen: `docker compose stop web api` [U]. Keep postgres up for forensics.
2. **Preserve evidence:** `docker compose logs --no-color api > incident-api.log`; export the audit rows for the affected org ids and request ids (query above, add `and org_id = '<uuid>'`).
3. **Reproduce on a copy, never on the live stack:** take a backup (backup-restore.md), restore it to a separate project, run the cross-tenant suites there:
   `bash scripts/dev/linux-runner/run.sh apps/api "python -m pytest -q -p no:cacheprovider ../../tests/security"` with `UKOS_RLS_COMPOSE_PROJECT`/ports of your own [V suite; U for this use].
4. **Understand the layers:** isolation is enforced by PostgreSQL row-level security (`org_id`, app role `ukos_app` is NOBYPASSRLS) first, then the API, and the RushDB index filters by tenant header. A leak through any one of them is a defect even if another layer held.
5. **Scope:** from audit/logs list which users read which org's resources (`target_type`, `target_id`, `org_id`), notify affected owners.
6. **Fix and regress:** add a failing test to `tests/security/**` first, fix, run the suite, then restart (`docker compose up -d`). Record the incident.

## 3. Connector compromise (a token for GitHub or another source is stolen, or a source syncs unwanted content)
1. **Revoke in the product:** `POST /api/v1/orgs/{org}/workspaces/{ws}/sources/{id}/revoke` (UI: source -> Revoke). This stops syncs, clears the stored token and invalidates memory built on that source's evidence (verified: `M06=1` smoke, state `revoked`, `has_token=false`) [V].
2. **Revoke at the provider** (the stored token may already be copied): GitHub -> Settings -> Developer settings -> tokens -> delete. [U]
3. **Quarantine content:** list documents from that source and delete the ones you do not trust (`DELETE .../documents/{id}`); deletion is immediate logical denial, purge after the grace period. [V]
4. **Rotate `CONNECTOR_ENCRYPTION_KEY`** if the key or `.env` may also have leaked (section 1).
5. Review audit events of the source id for what was synced and by whom. [U]

## 4. Deletion request (a person asks for their data to be removed)
1. Identify scope: documents (by uploader or workspace), memories, chat threads/runs, research sessions, connector sources.
2. Documents: `DELETE /api/v1/orgs/{org}/workspaces/{ws}/documents/{id}` -> 202 `deletion_requested` with `purge_after`; access ends immediately, derived chunks/vectors/graph claims/artifacts are purged by the worker, memories citing the document become `invalidated` [V: acceptance flow 9].
   To purge sooner than `DOCUMENT_PURGE_GRACE_DAYS` (default 7) set that variable lower and `docker compose up -d api worker` [U].
3. Memories: owner deletes (soft delete) in the UI/API; chat and research are private to their creator [V].
4. Verify: the document returns 404 for the owner, search no longer returns it, `select count(*) from document_versions where document_id='<id>' and purged_at is null` is 0 after the purge [U].
5. Backups still contain the data until they expire: state your backup retention to the requester (backup-restore.md).
6. Account removal: remove the user in the identity provider (Dex `staticPasswords` or your OIDC provider) after transferring ownership; an organization always keeps one owner. [U]
