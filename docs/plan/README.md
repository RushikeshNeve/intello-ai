# Universal Knowledge OS — Execution Pack

Architecture baseline v1.0 · 2026-11-16 · **Local-first, $0 recurring hosting**

**Status:** planned. This pack contains proposed implementation work, not completed engineering.

## Non-negotiable constraints
- PostgreSQL owns identity, grants, metadata, lifecycle, jobs, and publication state.
- RushDB 3 is a candidate tenant-isolated knowledge index (chunks, vectors, graph, evidence); validate first.
- S3-compatible storage owns original bytes; local provider remains to be validated.
- Redis/Celery are at-least-once workers; PostgreSQL outbox/checkpoints are durable truth.
- Explicit authorization, versioned evidence, safe deletion and no cross-tenant leakage.
- AI is evidence-first: hybrid retrieval, optional GraphRAG, citations and abstention.
- No paid infrastructure or required paid inference for the MVP. Future SaaS hosting is separate.

## Task execution policy
Each leaf task ID is the smallest assignable Claude-agent unit. The task owns its named path(s), implements its acceptance test, and submits a PR. **Tasks are not automatically parallel-safe:** obey dependencies and ownership. No agent may edit another task's owned paths without reallocation.

## Documents
- `MASTER_ARCHITECTURE.md`: contracts and decisions
- `AGENT_PLAYBOOK.md`: multi-agent orchestration and prompts
- `TASK_INDEX.csv`: importable backlog
- `M00`–`M08` milestone Markdown files: atomic work packages
- `ADR_REGISTER.md`: 57 decision index
