# Session Handoff — M02 complete, 2026-11-19 · next: M03

## State
- **M00 13/13, M01 12/12, M02 11/11 done; 36 of 108 tasks.** Exit gates passed for all three (`PROJECT_STATUS.md`). `main` is the source of truth; no agents are running; all agent branches are merged and their worktrees removed.
- **GitHub:** public repo `RushikeshNeve/intello-ai`, `main` pushed once at M02 batch B start (commit `b907303`). Later commits are local until the user asks for a push.
- **Product name:** Intello AI (internal prefix `ukos` stays until a planned rename task; see memory).
- The user's dev stack (`docker compose`, project `ukos`, ports 3300/8300/8380/…) was updated to the M02 code on 2026-11-19; migrations `0003` and `0004` applied on top of their data.
- What works end to end: sign-in → orgs/workspaces/roles → **upload documents straight to object storage with checksum verification, versions that can't be overwritten, quotas, library with search/filter/sort in the URL, a viewer for PDFs/images/text, deletion that denies access immediately**.

## Open items from M02 (carried, see `PROJECT_STATUS.md`)
1. Real-browser check of the PDF iframe and the text preview (CORS fetch): the user was asked to try these in the manual guide §5b.
2. Adversarial RLS tests for the four new tables (`documents`, `document_versions`, `upload_sessions`, `sources`): a test-only task, safe to run in parallel with M03 work.
3. Physical purge of deleted documents (object delete + `purged_at`) is a later job.
4. 56 style findings in older test folders (M08.03.01).

## Next: M03 — Reliable knowledge ingestion (14 tasks)
Plan: `docs/plan/M03_reliable_knowledge_ingestion.md`. The three chains (declared dependencies):
`M03.01.01 jobs + outbox schema → .02 outbox dispatcher → .03 Celery queues → .04 job claims, leases, heartbeats`;
`M03.02.01 extracted-content contract → .02 PDF/TXT/MD/HTML processors → .03 normalize + chunk → .04 checkpoints + fingerprints → .05 local embedding adapter`;
`M03.03.01 RushDB repository adapter → .02 versioned chunk indexing → .03 publication barrier → .04 ingestion status SSE + UI → .05 retry/dead-letter/crash-injection tests`.
Suggested start (write `docs/architecture/m03-contracts.md` first, as for M01/M02):
1. **Contract + migration (coordinator):** job/outbox/checkpoint tables (M03.01.01), the extracted-content and chunk shapes (M03.02.01), the RushDB record schema and the publication pointer in PostgreSQL. Honour the RushDB conditions in DC-009 (token only in the API/worker, tenant derived server-side, a single adapter injecting the workspace and published-revision filters, never `query.text`, cold-snapshot backups, text indexes re-created after restores).
2. **Parallel batch A:** M03.02.01→.02 (processors; pure Python, no infra) · M03.03.01 (RushDB adapter; needs the real RushDB, pinned 3.0.10) · M03.01.01→.03 (jobs, outbox dispatcher, Celery; Redis is a broker only, PostgreSQL is the truth) · the RLS-coverage test task.
3. Embedding adapter (M03.02.05) targets the OpenAI-compatible `/v1` API: LM Studio locally (`google/embedding-gemma-300m`, 768 dimensions, query/document prefixes in the adapter's model registry); the user also approved OpenAI as an optional provider (DC-020), key in `.env` as `OPENAI_API_KEY`, never in chat.

## Asks still open for the user
- GitHub: push M02 (and onward)? Automatic after each milestone, or on request?
- Feedback from the manual test (M01 and the §5b documents walkthrough).
- Sample corpus (prompt in `docs/development/sample-corpus-prompt.md`) before M04 evaluation work.
- Which connector first (Notion / Google Drive / GitHub) plus test credentials, by M06.

## Reminders (this machine)
- Run every test that imports psycopg, and mypy, through `scripts/dev/linux-runner/run.sh` (runbook §6); Node/HTTP suites and `python -m ruff` run on the host. Give each suite its own `UKOS_TEST_COMPOSE_PROJECT` and ports; never touch project `ukos` (the user's stack).
- Dex is pinned to v2.45.1. Quit Docker from the tray; if it crashes at start run `scripts/windows/fix-docker-sockets.ps1 -Start`.
- Weekend schedule note in memory; the user has been working on weekdays too.
