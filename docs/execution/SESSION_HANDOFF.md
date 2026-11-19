# Session Handoff — M05 complete, M06 in progress (2026-11-20)

## State
- **74 of 108 tasks done; gates passed M00–M05** (`PROJECT_STATUS.md`). `main` pushed to GitHub (b70a5c5) at M05 close. Push policy: automatic after each milestone gate (memory `push-after-each-milestone`).
- Working mode: **lean** (memory `token-budget`): agent briefs = short task + `docs/development/agent-rules.md`; sanity tests only; log skipped testing in `docs/execution/TEST_DEBT.md`; verify with `bash scripts/dev/verify.sh`. **UI polish is deferred to milestone M09** using `docs/ui-spec/*` (memory `ui-revamp-m09`). Plan: `docs/execution/REMAINING_PLAN.md`.
- Dev stack (compose project `ukos`) runs the latest main incl. `GRAPH_ENABLED=true` in the user's `.env`. Live smoke: `GRAPH=1 node tests/e2e/smoke_dev/smoke.mjs` (needs LM Studio serving google/gemma-3-4b + embedding-gemma-300m).

## In flight (check first)
- **M06 connectors merge agent** (`agent/m06-connectors-merged`, started 2026-11-19 night): reconciling `agent/m06-connectors` (29c990c: GitHub connector, sync, webhook, revoke, migration 0009) with main (13 conflicts; re-chain 0009 after 0008_graph; dedupe `documents/remote_ingest.ingest_bytes` vs `connectors/url_capture.create_document_from_bytes`). When it reports: fast-forward/merge into main, `bash scripts/dev/verify.sh`, RLS + m06_connectors suites, then rebuild dev stack and run a lean M06 smoke (fake GitHub server: new/changed/deleted files, webhook, revoke → docs 404, URL capture on a public fixture), tell the user to set a real `CONNECTOR_ENCRYPTION_KEY` (random 32 bytes base64) in `.env` for token connectors, update PROJECT_STATUS (M06 → 85/108), push.
- M06 formats/URL capture is already on main (47 tests); not yet exercised end to end on the live stack.

## Next
1. Finish M06 (above). 2. **M07** (memory/research/UX functionality; slim contract + 3 agents per `REMAINING_PLAN.md`; functionality only). 3. **M08** (lean hardening: CI gates, backup/restore, tracing, runbooks, tag baseline). 4. **M09** UI/UX revamp from the user's specs.
- Cleanup after M06 merges: `docker builder prune`, `docker container prune`, remove merged agent worktrees (22 listed); ask before `docker image prune -a`. Never prune volumes or touch project `ukos`.

## Open asks for the user
- Feedback from manual tests (M02 §5b previews, M03 §5c status UI, M04 §5d search/chat, M05 explorer, real browser).
- Sample corpus (`docs/development/sample-corpus-prompt.md`) for real-document evaluation (needs a small converter to the eval format).
- Product decision: hide Retry for `unsupported` files? (shown today).
- User must clear the Claude app's Cowork image (9.5 GB) in Settings → Storage themselves.

## Reminders (this machine)
- psycopg tests and mypy only via `scripts/dev/linux-runner/run.sh` (set `MYPYPATH` INSIDE the quoted command); Node suites and `python -m ruff` on host. Own `UKOS_TEST_COMPOSE_PROJECT` + ports per suite; never touch project `ukos`.
- Known gaps are in `TEST_DEBT.md` (real-browser passes, verification tuning, hostile-chunk detection, RushDB vector stats after delete, etc.).
