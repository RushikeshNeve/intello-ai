# Public release checklist (M10)

Tick each item with evidence (command output, screenshot, commit). Nothing is released until M09 (UI revamp) is accepted and the user approves the target and any cost.

## A. Decisions (user)
- [ ] Hosting target approved (default: one free VM, `docs/runbooks/deploy-free-vm.md`) and cost ceiling stated (`docs/release/FREE_VM_COST_LIMITS.md`)
- [ ] Domain chosen; app and files hostnames resolve to the VM
- [ ] AI provider chosen, monthly budget cap set at the provider, data-use policy accepted
- [ ] Who may sign in (Dex admin only vs invited users) and how users are added
- [ ] Public vs invite-only; terms/privacy text if public

## B. Security
- [ ] `scripts/deploy/preflight.sh` passes; `.env.prod` is mode 600, not in git, secrets in a password manager
- [ ] Only 80/443 reachable from the internet (`nmap` from outside); SSH key-only and restricted; no Postgres/Redis/RushDB/S3 ports public
- [ ] Dex has no test users and no password grant (`dex.prod.yaml`); admin password is long and unique
- [ ] Cookies are `__Host-`/Secure; HSTS present (smoke test)
- [ ] Cross-tenant isolation suites green on the release commit (RLS, API, e2e); no known open S1/S2 security items
- [ ] Rate limits and upload/quota limits reviewed for public exposure (`SEARCH_RATE_PER_MIN`, `CHAT_MAX_RUNS_PER_MIN`, `UPLOAD_MAX_BYTES`, `ORG_STORAGE_QUOTA_BYTES`)
- [ ] AI key budget cap in place; external-provider gating (`AI_EXTERNAL_PROVIDERS_ENABLED`) intentional
- [ ] Dependency/image scan run (`docs/release/CI.md`); pinned image versions reviewed

## C. Deployment
- [ ] arm64 image build succeeded on the VM; all services healthy
- [ ] `tests/e2e/prod_smoke/smoke.mjs` passes (with and without credentials)
- [ ] Manual pass: sign in, create org and workspace, upload, ingestion ready, search, grounded chat with citation, second user cannot see it
- [ ] Rollback rehearsed once (`scripts/deploy/rollback.sh`) and the previous tag verified healthy

## D. Data safety
- [ ] Daily backup scheduled; retention set; off-VM copy configured and verified
- [ ] One full restore rehearsal on a scratch stack from an off-VM copy (`docs/runbooks/backup-restore.md`)
- [ ] Disk, log and image growth reviewed after a week; alert or reminder to check

## E. Operations
- [ ] External uptime check on `/api/health` (optional) and an owner who looks at it
- [ ] Update procedure documented and exercised once (`deploy.sh` with a new tag)
- [ ] Incident contacts and `docs/runbooks/security-incident.md` reviewed
- [ ] Known limits communicated (single VM, no HA, free-tier risks)

## F. Product and docs
- [ ] M09 accepted; release notes written; version tag created from the deployed commit
- [ ] User-facing docs/onboarding adequate for first external users
- [ ] Support path (email/issues) stated
