# Test debt (deferred thorough testing: planned for the 2-day testing pass next week)

**Policy (user decision 2026-11-19):** this week = finish features with sanity tests only (happy path, one cross-tenant check per new route/table, one smoke gate per milestone). Thorough testing is a dedicated pass afterwards.

## Deferred, by area (add a line when you skip something)
- M04: chat runtime/web had no exhaustive failure-injection or fuzzing; real-browser pass of search/chat/evidence UI; retrieval quality with real embeddings on the user's sample corpus; verification threshold tuning (two known misses: synonym paraphrase over-withheld, "every year" vs "every four years" leaks); hostile-chunk detection (a retrieved chunk that really contains the claim passes verification).
- M03: real-browser pass of the status UI; throughput with real LM Studio latency; RushDB vector stats don't drop after delete (physical removal); "worker killed between PG commit and flag flip" not timed directly.
- M02: PDF iframe and text preview in a real browser (manual guide §5b).
- Security: PK-collision existence oracle; 24 ruff + 4 mypy findings in old security tests; 56 older style findings (M08.03.01).
- Retry button shown for `unsupported` files (decide in M06).
- M05 integration: no HTTP-stub web e2e for the graph explorer (React Flow canvas is stubbed in vitest; real-browser pass of canvas keyboard focus and edge click pending); entity name search scans up to 5000 active entities per request in RushDB (no proven substring operator), revisit with a native prefix/contains filter.
- M06: webhook/HMAC and token connectors not run on the live stack (needs CONNECTOR_ENCRYPTION_KEY); OCR/audio engines only fakes; code-file repository locators and xlsx not exercised live; sync scheduler tick timing; `.env.example` placeholder CONNECTOR_ENCRYPTION_KEY is not valid base64 (cipher rejects `change-me*` by design); web UI for sources absent (API only); first LM Studio call after idle times out (consider warm-up/longer first-call timeout).

- M07 web (memory/research/palette/locator viewers/theme): no HTTP e2e suite; covered by vitest with stub APIs only. Gap: live BFF forwarding of `/api/v1/orgs/*/memory*` and `research*` (generic proxy, verified by reading bff.ts only), real-browser palette/focus-trap and 320 px layout checks.
- M07: web research-limits keys (`max_subqueries|max_evidence|max_model_calls|timeout_seconds`) and memory/research list pagination param `after` were assumed by the web agent: verify against the live API in a real browser; chat memory is injected as pseudo-evidence cited by numeric marker; `revoke_source` -> memory invalidation hook untested end to end; command palette, theme switch, memory/research screens have only vitest coverage (no browser); research uses the chat runtime factory (one Celery pool shared with ingest/chat).

- Stale seed in `tests/integration/m01_foundation/m01_seed.py`: `test_rls_smoke.py::test_org_context_isolates_orgs` and one m02_foundation test fail because the seed creates no `ai_policies` row and a `sources` row violating `ck_sources_connector_account` (tables added in M04/M06; seed never extended). Unrelated to preapprovals; fix in the testing sprint.
