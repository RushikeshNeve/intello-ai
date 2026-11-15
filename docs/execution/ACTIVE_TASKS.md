# Active Tasks

Statuses: `TODO → READY → IN_PROGRESS → REVIEW → DONE`, plus `BLOCKED` / `FAILED`.
A task is DONE only after the coordinator reruns its acceptance check on `main` (evidence in `AGENT_HANDOFFS.md`).

## M00 — Batch 1 (started 2026-11-16, **paused** — see SESSION_HANDOFF.md)

| Task | Title | Owner (agent) | Branch | Practical deps | Status |
|---|---|---|---|---|---|
| M00.01.01 | System boundaries + ADR index | coordinator | main | — | DONE |
| M00.01.02 | Local env contract | coordinator | main | — | REVIEW (finalized after all compose files land) |
| M00.02.01 | Compose PostgreSQL + Redis | platform-agent | agent/m00-platform | M00.01.02 | REVIEW (committed f6d04a8, not yet verified by coordinator) |
| M00.02.05 | FastAPI + Next.js starters + apps.yml | platform-agent | agent/m00-platform | M00.02.01 | IN_PROGRESS (paused, uncommitted WIP) |
| M00.02.02 | Compose RushDB with persistence | rushdb-agent | agent/m00-rushdb | M00.01.02 | REVIEW (committed 79856e1, unverified) |
| M00.03.01 | RushDB tenant isolation spike | rushdb-agent | agent/m00-rushdb | M00.02.02 | REVIEW (committed 6721608, unverified) |
| M00.03.02 | RushDB hybrid retrieval spike | rushdb-agent | agent/m00-rushdb | M00.02.02 | REVIEW (committed b45e8f5, unverified) |
| M00.03.03 | Graph provenance spike | rushdb-agent | agent/m00-rushdb | M00.02.02 | IN_PROGRESS (tests committed 0ddb6fa; GO/NO-GO doc not written) |
| M00.02.03 | Evaluate local S3-compatible storage | objectstore-agent | agent/m00-objectstore | — | IN_PROGRESS (paused, uncommitted WIP) |
| M00.02.04 | Compose local object store | objectstore-agent | agent/m00-objectstore | M00.02.03 | TODO |
| M00.03.04 | Benchmark local embeddings + generation | models-agent | agent/m00-models | — | IN_PROGRESS (paused, uncommitted WIP) |
| M00.03.05 | Backup/restore smoke | coordinator | main | 02.01, 02.02, 02.04 | IN_PROGRESS (draft, not run) |
| M00.01.03 | Developer runbook | coordinator | main | 02.01–02.05 | TODO |

### Why these groupings are parallel-safe
- The four agents own disjoint paths: `data.yml`/`apps.yml`/`apps/**`, `rushdb.yml`/`tests/spikes/rushdb_*`, `objects.yml`/`docs/spikes/object-storage.md`/`tests/spikes/objectstore*`, `docs/spikes/local-models.md`/`tests/spikes/models*`.
- None of them edits `infrastructure/env/.env.example`, root `compose.yaml`, or `docs/execution/*`. They report needed env changes in their handoff, and the coordinator applies them.
- Each agent runs Docker under its own compose project name (`ukos-<agent>`) on its own host ports from the UKOS block, so containers and volumes never collide.
- Tasks that share a service (the three RushDB spikes) run sequentially inside one agent because they share a fixture helper.
