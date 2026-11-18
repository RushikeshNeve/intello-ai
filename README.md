# Intello AI

A local-first, multi-tenant AI knowledge management system: upload documents, search them, and chat with answers that
are grounded in your own sources, with citations and an honest "I don't know". Runs on your own machine with no
mandatory paid services.

> **Status: work in progress.** The foundations and the first features are built and tested; search and chat are not
> yet. See [`docs/execution/PROJECT_STATUS.md`](docs/execution/PROJECT_STATUS.md) for the live numbers.
> The code and docs still use the working name *Universal Knowledge OS* and the internal prefix `ukos`.

## What works today
- **Identity and tenancy:** sign-in through a local OIDC provider, organizations, workspaces, roles, document sharing grants, audit trail, and PostgreSQL row-level security with an adversarial test suite.
- **Web app:** sign-in with HttpOnly sessions, an organization switcher, workspace navigation and deep links.
- **Documents (in progress):** direct-to-storage uploads with checksum verification, immutable versions, quotas, and deletion that denies access immediately.

## Stack
Next.js (App Router, TypeScript, Tailwind, shadcn/ui) · FastAPI modular monolith · PostgreSQL · RushDB 3 (knowledge index) ·
S3-compatible object storage (RustFS) · Redis + Celery · Dex (OIDC) · local inference through an OpenAI-compatible server
(LM Studio in development) · Docker Compose.

## Run it
```bash
cp infrastructure/env/.env.example .env     # then replace the change-me-* values
docker compose up -d --wait                  # about 30 s once images are built
# web: http://127.0.0.1:3300   API docs: http://127.0.0.1:8300/docs
```
Step-by-step setup: [`docs/development/local-runbook.md`](docs/development/local-runbook.md).
Hands-on walkthrough with test accounts: [`docs/development/manual-test-guide.md`](docs/development/manual-test-guide.md).

## Documentation
- Plan and decisions: [`docs/plan/`](docs/plan/README.md) (architecture, 57 ADRs, 108 tasks)
- Architecture boundaries: [`docs/architecture/system-boundaries.md`](docs/architecture/system-boundaries.md) and the per-milestone contracts in `docs/architecture/`
- Progress and decision log: [`docs/execution/`](docs/execution/)

## License
Not yet chosen.
