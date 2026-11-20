# Runbook: deploy the full stack on one free VM

Status: **prepared, UNTESTED on a real VM.** Nothing here has been run against a cloud account. Validated locally only:
`docker compose config` of the prod stack, preflight script on a sample env, web session-store unit tests.
Where a step is a guess about a provider's console, it says "verify". Cost/limit facts: `docs/release/FREE_VM_COST_LIMITS.md`.

Target: one VM (example: Oracle Cloud Always Free Ampere A1, up to 4 OCPU / 24 GB, arm64) running the same Compose stack as dev
(Postgres, RushDB, Redis, RustFS, Dex, API, worker, dispatcher, web) with Caddy terminating TLS. Only ports 80/443 are public.

## 0. What you need before starting
- A cloud account for the VM (you create it; card verification is usual even for free tiers: verify).
- Two DNS names pointing at the VM: the app (`app.<domain>`) and the object store (`files.<domain>`).
- An AI provider key (default: OpenAI-compatible hosted API; usage is billed by the provider, not free).
- A password manager for the generated secrets.

## 1. Provision the VM
1. Create an Ubuntu 24.04 (arm64) instance, shape with at least 2 OCPU / 12 GB (4 / 24 is what the resource limits in `prod.yml` assume), 100+ GB boot volume.
   "Out of capacity" errors for free ARM shapes are common in some regions: retry later or another availability domain (verify).
2. Add your SSH public key. Note the public IPv4.
3. Network: in the cloud firewall (security list / NSG) allow inbound TCP 22 (ideally only your IP), 80, 443 and UDP 443. Nothing else.
4. On the VM also open the OS firewall for the same ports. Oracle's Ubuntu images ship restrictive iptables rules: `sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT`, same for 443 and `-p udp --dport 443`, then persist (`sudo apt install -y iptables-persistent && sudo netfilter-persistent save`). (Verify on your image.)

## 2. DNS and domain
- Best: a domain you own (any registrar). Create A records `app` and `files` -> VM public IP. If you use IPv6, add AAAA too.
- Free option: DuckDNS (`yourname.duckdns.org`). You need TWO names (e.g. `ukos-app.duckdns.org`, `ukos-files.duckdns.org`; DuckDNS allows several per account: verify the current limit) because presigned object URLs need their own host. Do not rely on sub-subdomains.
- A reserved/static public IP is strongly recommended; if the IP changes, DNS breaks and certificates cannot renew.
- Check: `dig +short app.<domain>` returns the VM IP before you start Caddy, or Let's Encrypt validation fails (rate limits apply).

## 3. Install Docker and tools
```bash
sudo apt update && sudo apt install -y git curl ca-certificates openssl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
docker compose version      # needs v2.24+ (the overlay uses !reset)
node --version              # only needed to run the smoke test / ops scripts from the VM: sudo apt install -y nodejs
```
Optional off-VM backups: `sudo apt install -y rclone` (then `rclone config`).

## 4. Get the code
```bash
git clone https://github.com/<your-account>/intello-ai.git && cd intello-ai
```
(Use your private repo URL with a deploy key or a fine-grained read-only token; never put a token in the URL you paste into chat.)

## 5. Fill the environment
```bash
cp infrastructure/env/.env.prod.example .env.prod && chmod 600 .env.prod
```
Edit `.env.prod`. Every `CHANGE_ME*` has its generation command in the comment above it (`openssl rand -hex 32`, the bcrypt command for the Dex admin password, `cat /proc/sys/kernel/random/uuid`, ...). Set `PUBLIC_HOST`, `FILES_HOST`, `PUBLIC_BASE_URL`, `S3_PUBLIC_ENDPOINT_URL` (the first and last two must match: preflight checks), `ACME_EMAIL`, the AI key.
Save the secrets in your password manager. `.env.prod` is git-ignored; never commit or paste it.

## 6. Preflight
```bash
bash scripts/deploy/preflight.sh
```
Fix every `FAIL` line. It prints names and problems, never secret values.

## 7. First deploy
```bash
bash scripts/deploy/deploy.sh
```
Builds the API and web images on the VM (first build on ARM: 10+ minutes; if a Python dependency has no arm64 wheel the build fails: report the package, untested), starts the data services, runs `api-migrate`, creates buckets, starts everything, waits for healthy. Caddy requests the certificates on first start (needs 80/443 reachable).
Check: `docker compose -p ukos-prod --env-file .env.prod -f infrastructure/compose/data.yml ... ps` (or `source scripts/deploy/lib.sh; dc ps`).

## 8. Smoke test (from your laptop or the VM)
```bash
BASE_URL=https://app.<domain> FILES_URL=https://files.<domain> node tests/e2e/prod_smoke/smoke.mjs
# with sign-in:  SMOKE_USER=<dex admin email> SMOKE_PASSWORD='<password>' ...   (do not leave the password in shell history: use read -s)
```
Then sign in with the browser as the Dex admin, create an organization and workspace, upload a small text file, wait for "ready", search, ask a question. That manual pass also exercises the AI provider and the object-store host, which the scripted smoke does not.

## 9. Backups
```bash
bash scripts/deploy/backup-cron.sh                  # one backup now (pg_dump + RushDB cold snapshot + S3 export + manifest)
bash scripts/deploy/backup-cron.sh --install-cron   # prints the crontab line; add it with `crontab -e`
```
Retention: newest `BACKUP_KEEP` (default 7) in `BACKUP_DIR`. Off-VM: set `BACKUP_RCLONE_REMOTE` (e.g. an R2/B2/Drive remote); the script copies and verifies after each run. **A backup that lives only on the VM does not survive losing the VM.** Do one restore rehearsal (`docs/runbooks/backup-restore.md`) on a scratch stack before trusting it. Note: each run stops RushDB for a few seconds.

## 10. Update
```bash
bash scripts/deploy/deploy.sh      # git pull --ff-only, pre-deploy backup, build with a new tag, migrate, restart, wait healthy
node tests/e2e/prod_smoke/smoke.mjs
```

## 11. Roll back
```bash
bash scripts/deploy/rollback.sh        # previous image tag, no rebuild, no migration
```
Code only. If the bad release applied a non-backwards-compatible migration, restore from the pre-deploy backup instead. Old images stay on disk (tags `ukos-api:<sha>`, `ukos-web:<sha>`); prune manually with `docker image prune` once you no longer need them.

## Operations notes
- Logs: `dc logs -f api` (json-file, 10 MB x 5 per container). Disk: watch `df -h` and `docker system df`.
- Resource limits live in `infrastructure/compose/prod.yml`; lower them for smaller shapes (RushDB wants >= 1 GB).
- Free providers may reclaim idle instances: see the limits sheet; keep the VM lightly active and keep off-VM backups.
- Rotating a secret: edit `.env.prod`, `bash scripts/deploy/deploy.sh --no-pull` (recreates affected containers). Changing the Postgres/RushDB/S3 secrets of an existing volume needs the matching in-service change first.
- Switching the AI model/dimensions on a populated deployment = new embedding space: plan a re-embed first.

## Known untested items (verify at first deploy)
1. arm64: Python wheel installs during image build; RushDB and RustFS runtime on arm64 (images are multi-arch, behavior not run).
2. Hairpin TLS: api/web reach `https://PUBLIC_HOST/dex` through Caddy's network alias; certificate must already be valid, so first boot may log OIDC errors until Caddy has issued it.
3. `RespClient` (web session store) against a real Redis with AUTH and database selection; unit tests use a fake client.
4. Presigned browser uploads to `https://FILES_HOST` (CORS applied by `objectstore-init` from `API_CORS_ORIGINS`).
5. Hosted-embedding behavior with empty prefixes and 1536 dimensions; chat via `OPENAI_API_KEY`/`AI_API_KEY`.
6. The backup script against the prod file set (`OPS_COMPOSE_FILES` hook) and `rclone` off-VM copy.
