# Decision Changes and Spec Interpretations

Records every point where implementation deviates from, clarifies, or interprets the execution pack
(`docs/plan/`). **Architecture changes** (anything that alters an ADR) need the user's approval. Those entries are
marked `NEEDS APPROVAL` until approved.

| ID | Date | Type | Decision | Status |
|---|---|---|---|---|
| DC-001 | 2026-11-16 | Fact correction | RushDB 3.x is a standalone engine (Apache-2.0, image `rushdb/rushdb:3`), **not** the Neo4j-based 2.x stack. It advertises fail-closed tenants with their own keyspaces, BM25, exact/HNSW vectors over caller-supplied vectors, hybrid fusion, and Cypher/Gremlin/JSON queries. It does **not** generate embeddings (we supply vectors, consistent with ADR-020). The M00.03 spikes validate these claims. | Recorded |
| DC-002 | 2026-11-16 | Open selection | MinIO Community Edition is retired upstream (archived, source-only), so it is not a default candidate. M00.02.03 evaluates SeaweedFS (Apache-2.0), RustFS (Apache-2.0) and Garage (AGPL-3.0) against the S3 subset we need. The compose service is named `objectstore` and the env names are provider-neutral (`S3_*`), so the choice stays swappable (ADR-007/051). | Pending M00.02.03 |
| DC-003 | 2026-11-16 | Local-ops convention | UKOS host-port block: web 3300, api 8300, rushdb 3310, objects 9300, postgres 5532, redis 6479, oidc 8380, ollama 11434, all bound to 127.0.0.1. Reason: other local projects on this machine publish 3000/5432/6379/8000 on 0.0.0.0. Container-internal ports stay standard. | Applied |
| DC-004 | 2026-11-16 | Ownership | Overlapping owned paths are serialized. See "Ownership conflicts" in `DEPENDENCY_GRAPH.md`. | Applied |
| DC-005 | 2026-11-16 | Scope clarification | No task owns the API/web starter projects that M00.02.05 needs to prove health endpoints. M00.02.05's owned paths are extended to `apps/api/` (pyproject, `app/main.py`, health router, Dockerfile, tests) and `apps/web/` (Next.js starter, health route, Dockerfile). Later tasks own their own module paths inside these. | Applied |
| DC-006 | 2026-11-16 | Ownership | Root `compose.yaml` (includes `infrastructure/compose/*.yml`), root `.gitignore`/README and future package manifests belong to the integration owner (coordinator). | Applied |
| DC-007 | 2026-11-16 | Gate definition | Exit gates defined in `DEPENDENCY_GRAPH.md`. A RushDB NO-GO halts M03+ RushDB-dependent work and goes to the user. | Applied |
| DC-008 | 2026-11-16 | Process | The Agent Playbook asks for one PR per task. There is no GitHub remote yet, so each agent commits to `agent/<task-id>` in its own worktree, and the coordinator reviews and merges locally into `main` with `--no-ff`. If a remote is added later, PRs replace local merges. | Applied |
