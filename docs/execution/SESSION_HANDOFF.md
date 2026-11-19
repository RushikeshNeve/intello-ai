# Session Handoff — M03 complete, 2026-11-19 · next: M04

## State
- **M00 13/13, M01 12/12, M02 11/11, M03 14/14 done; 50 of 108 tasks.** Exit gates passed for all four (`PROJECT_STATUS.md`). `main` is the source of truth; no agents running.
- **GitHub:** public repo `RushikeshNeve/intello-ai`; the user said "Yes, push" for M02 only. M03 commits are local until the user asks. Ask whether to push after each milestone.
- Product: Intello AI (internal prefix `ukos`).
- Works end to end: sign-in → orgs/workspaces → upload → **background ingestion (extract, chunk, embed, index, publish) with live status in the UI, retry, crash recovery, deletion purge**. No search or chat yet (M04).

## Dev stack (user's, compose project `ukos`)
Needs `docker compose up -d --build` (migrations 0005/0006 create the `ukos_worker` role from `POSTGRES_WORKER_*` in `.env`, which the user has set; new `worker` + `dispatcher` services; the M02 first-upload ON CONFLICT fix). LM Studio must serve `google/embedding-gemma-300m` at 127.0.0.1:1234 for real embeddings. Manual steps: `docs/development/manual-test-guide.md` §5c.

## Next: M04 — Search & grounded chat (13 tasks)
Plan `docs/plan/M04_*.md`. Write `docs/architecture/m04-contracts.md` first (as for M01–M03). Retrieval must use `KnowledgeRepository.search_*` with `SearchFilters` and revalidate each hit with `is_revision_published(...)`. OpenAI is optional (DC-020; key in `.env` as `OPENAI_API_KEY`, set by the user; never print it); local LM Studio default. The golden evaluation set needs the user's sample corpus (`docs/development/sample-corpus-prompt.md`): ask for it.

## Open asks for the user
- Push M03 (and onward)? Automatic after each milestone, or on request?
- Feedback from manual tests (M02 §5b PDF/text preview; M03 §5c status UI).
- Sample corpus before M04 evaluation work; connector choice (Notion / Google Drive / GitHub) + credentials by M06.
- Product decision: hide Retry for `unsupported` files? (currently shown; useless until M06).

## Reminders (this machine)
- psycopg tests and mypy only via `scripts/dev/linux-runner/run.sh` (runbook §6; set `MYPYPATH` INSIDE the quoted command); Node suites and `python -m ruff` on the host. Own `UKOS_TEST_COMPOSE_PROJECT` + ports per suite; never touch project `ukos`.
- Dex pinned v2.45.1; Docker crash fix `scripts/windows/fix-docker-sockets.ps1 -Start`.
- Known M08 items: RushDB vector stats don't drop after delete (DC-024(7)); PK-collision existence oracle; 24 ruff + 4 mypy pre-existing findings in old security tests; 56 older style findings.
