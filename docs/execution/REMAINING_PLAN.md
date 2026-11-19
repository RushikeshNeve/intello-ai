# Remaining plan (M04 close → M08), lean mode (see memory `token-budget`, `docs/execution/TEST_DEBT.md`)

Rules: ALL tasks built; sanity tests only; one slim contract per milestone (≤ 100 lines, coordinator); 3 agents per milestone (backend A, backend B, web); one smoke gate per milestone; briefs = 10-line task + `docs/development/agent-rules.md`; handoffs ≤ 25 lines; push after each gate.

| Step | Scope | Agents |
|---|---|---|
| M04 close | merge chat runtime + web; smoke gate (stub model, then one real LM Studio run); trackers; push | 0 new (2 running) |
| M05 graph (11) | A: ontology + extraction + resolution + claim/evidence edges (M05.01.*); B: permission-safe traversal + selective router + evidence merge + bounded neighborhood API + hybrid-vs-graph benchmark (M05.02.*, .03.01, .03.04); W: React Flow explorer + edge provenance (M05.03.02-.03) | 3 |
| M06 connectors/formats (11) | A: connector port, cursor, token encryption, scheduler/webhook, first connector, revocation (M06.01.01,.03-.06); B: safe URL capture + CSV/XLSX + repo locators + OCR/audio/video contracts (M06.01.02, M06.02.*); W: only if needed (connector UI inside B) | 2-3 |
| M07 memory/research/UX (11) | A: memory model, APIs, invalidation (M07.01.01-.03); B: research sessions, planner, reports (M07.02.01-.03); W: memory UI, research workspace, command palette, code/sheet viewers, light/dark (M07.01.04, M07.02.04, M07.03.*) | 3 |
| M08 hardening/release (12) | A: lean adversarial + deletion-lineage + credential tests (M08.01.*); B: backup automation + restore rehearsal + tracing + minimal load test (M08.02.*); C: CI gates, install/security runbooks, acceptance checklist, tag baseline, deferrals (M08.03.*) | 3 |

Dependencies that are only formal (not technical): M06.01/.02 do not need the graph, so M06 batch A can start while M05 UI is finishing. Decisions needed from the user: first connector (see below) and its test credentials by M06.
