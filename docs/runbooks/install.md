# Runbook: clean-machine install (Windows 11 + Docker Desktop + LM Studio)

Result: the full stack on one Windows machine, local only (every port bound to 127.0.0.1), $0 recurring cost.
Companion docs: `docs/development/local-runbook.md` (developer detail), `operations.md`, `troubleshooting.md`.

Command status legend: **[V]** verified on the project machine during development (cited in `docs/development/local-runbook.md`
or run by the acceptance suite), **[T]** run while writing this runbook, **[U]** UNTESTED (written from the docs of the tool).

## 1. Prerequisites and versions

| Tool | Version used | Install |
|---|---|---|
| Windows | 11 (build 26200) | WSL 2 enabled (`wsl --install`, reboot) [U] |
| Docker Desktop | Engine 29.3.1, Compose v5.1.1 | docker.com installer, WSL 2 backend; give the VM at least 8 GB RAM (12 GB with local AI) |
| Git for Windows (Git Bash) | 2.5x | git-scm.com |
| Node.js | 24.x (only needed to run the acceptance/smoke scripts) | nodejs.org |
| Python 3.12 + uv 0.11.x | only for development, not needed to run the stack | python.org installer (not uv-managed Python: see troubleshooting 1) |
| LM Studio | 0.4.9 with the `lms` CLI | lmstudio.ai (free for personal/internal use only) |

Run all commands below in **Git Bash** from the repository root, with `export MSYS_NO_PATHCONV=1` first.

## 2. Get the code and create `.env`

```bash
git clone <repo-url> universal-knowledge-os && cd universal-knowledge-os     # [V]
cp infrastructure/env/.env.example .env                                      # [V]  (.env is git-ignored)
```

Replace **every** `change-me-*` value in `.env` before the first start (passwords are fixed into the volumes on first start).

Generate secrets (any of these; output is the secret, do not paste it into chat or commit it):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"      # passwords, API_SECRET_KEY, RUSHDB_TOKEN, S3 keys  [V]
node -e "console.log(require('crypto').randomBytes(36).toString('base64url'))"   # WEB_SESSION_SECRET (>= 32 chars)  [V]
openssl rand -base64 32                                           # CONNECTOR_ENCRYPTION_KEY: base64 of 32 random bytes  [U: openssl ships with Git Bash]
node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"      # same thing without openssl  [T]
```

| Variable(s) | Rule |
|---|---|
| `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`, `POSTGRES_WORKER_PASSWORD` | random; then put the SAME passwords into `DATABASE_URL`, `DATABASE_MIGRATION_URL`, `DATABASE_WORKER_URL` |
| `REDIS_PASSWORD` | random; same value inside `REDIS_URL` and `CELERY_BROKER_URL` |
| `RUSHDB_TOKEN`, `API_SECRET_KEY`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | random |
| `WEB_SESSION_SECRET` | random, at least 32 characters |
| `OIDC_CLIENT_SECRET` | random |
| `CONNECTOR_ENCRYPTION_KEY` | base64 of 32 random bytes; keep `CONNECTOR_ENCRYPTION_KEY_ID=k1`. Needed only to store connector tokens. Losing it makes stored connector tokens unreadable (re-enter them) |
| `AI_BASE_URL` | `http://host.docker.internal:1234/v1` (LM Studio on the host) |
| `GRAPH_ENABLED` | `true` to build the knowledge graph (needs the chat model); default `false` |
| `OPENAI_API_KEY`, `AI_EXTERNAL_PROVIDERS_ENABLED` | leave empty/false for a fully local setup |

Never commit `.env`. Back it up in your password manager: the database passwords cannot be recovered from the volumes.

Check that nothing is left: `grep -n "change-me" .env` must print nothing. [T]

## 3. Local sign-in users

The bundled Dex identity provider ships three TEST-ONLY users (alice/bob/mallory `@ukos.test`, passwords in
`infrastructure/compose/identity/README.md`). For anything beyond a personal trial, edit
`infrastructure/compose/identity/dex.yaml` (`staticPasswords`: bcrypt hash via `htpasswd -bnBC 10 "" 'password' | tr -d ':\n'`
[U]) or point the web app at another OIDC provider (`OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`). Restart `dex` after a change.

## 4. Start the stack

```bash
docker compose up -d --wait        # [V] ~30 s warm; first build of the api/web/worker images takes several minutes
docker compose ps                  # [V] api-migrate and objectstore-init show "exited (0)": that is correct
curl -fsS http://127.0.0.1:8300/health/ready     # [V] {"status":"ok",...}
curl -fsS http://127.0.0.1:3300/api/health       # [V] {"status":"ok"}
```

Open http://127.0.0.1:3300 and sign in. The first time, create an organization and a workspace.
Ports: web 3300, api 8300, rushdb 3310, objects 9300, postgres 5532, redis 6479, dex 8380, LM Studio 1234.

## 5. Local AI (LM Studio)

```bash
lms get google/embedding-gemma-300m                  # [V] about 229 MB, 768 dimensions
lms get google/gemma-3-4b                            # [V] about 3.3 GB
lms load google/embedding-gemma-300m --gpu max -y    # [V]
lms load google/gemma-3-4b --gpu max --context-length 4096 -y   # [V]
lms server start                                     # [V] http://127.0.0.1:1234
curl -fsS http://127.0.0.1:1234/v1/models            # [V] both models listed
```

If `lms` is not found use `%USERPROFILE%\.lmstudio\bin\lms.exe`. Without a GPU the same models work, about 4x slower.
**Do not change the embedding model after documents are ingested**: it defines the embedding space (id `eb3ad37f8d916` for
the default model, prefixes and dimensions); a different model means re-embedding everything.

## 6. Verify the install end to end

```bash
node tests/e2e/smoke_dev/smoke.mjs        # [V] sign-in, upload, ready, search, grounded chat (needs LM Studio). Creates one org "M04 smoke ..."
```

Optional flags: `GRAPH=1`, `M07=1`, `M06=1` (needs internet). It uses the test users above, so it works only with the bundled Dex users.
Without LM Studio run the stub-based suite instead (builds a separate project, does not touch yours):
`bash tests/e2e/acceptance/run.sh` (see `docs/release/CHECKLIST.md`).

## 7. Stop, update, remove

```bash
docker compose stop                      # [V] keeps data
docker compose up -d --build --wait      # [V] after `git pull`: rebuilds images, api-migrate applies new migrations
docker compose down                      # [V] keeps volumes
docker compose down -v                   # [V] DELETES ALL DATA (databases, objects, graph). Back up first: backup-restore.md
```

After `git pull`, read `docs/release/BASELINE.md` and the migration list; never edit `.env` passwords after first start
(wipe the volumes or change the role passwords by hand).
