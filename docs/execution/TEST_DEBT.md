# Test debt (deferred thorough testing: planned for the 2-day testing pass next week)

**Policy (user decision 2026-11-19):** this week = finish features with sanity tests only (happy path, one cross-tenant check per new route/table, one smoke gate per milestone). Thorough testing is a dedicated pass afterwards.

## Deferred, by area (add a line when you skip something)
- M04: chat runtime/web had no exhaustive failure-injection or fuzzing; real-browser pass of search/chat/evidence UI; retrieval quality with real embeddings on the user's sample corpus; verification threshold tuning (two known misses: synonym paraphrase over-withheld, "every year" vs "every four years" leaks); hostile-chunk detection (a retrieved chunk that really contains the claim passes verification).
- M03: real-browser pass of the status UI; throughput with real LM Studio latency; RushDB vector stats don't drop after delete (physical removal); "worker killed between PG commit and flag flip" not timed directly.
- M02: PDF iframe and text preview in a real browser (manual guide §5b).
- Security: PK-collision existence oracle; 24 ruff + 4 mypy findings in old security tests; 56 older style findings (M08.03.01).
- Retry button shown for `unsupported` files (decide in M06).
- M05 integration: no HTTP-stub web e2e for the graph explorer (React Flow canvas is stubbed in vitest; real-browser pass of canvas keyboard focus and edge click pending); entity name search scans up to 5000 active entities per request in RushDB (no proven substring operator), revisit with a native prefix/contains filter.
