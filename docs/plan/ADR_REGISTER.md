# ADR Register — 001 through 057

Status: provisional architecture baseline. These are decision summaries, not full ADR rationale documents.

## Product boundaries

| ID | Decision | Status |
|---|---|---|
| ADR-001 | Phased individuals/teams/enterprise | Accepted provisionally |
| ADR-002 | Cloud SaaS product target | Accepted provisionally |
| ADR-003 | Initial 100–1,000-user hosted target | Accepted provisionally |
| ADR-004 | Shared multi-tenancy | Accepted provisionally |

## Ingestion and storage

| ID | Decision | Status |
|---|---|---|
| ADR-005 | Universal-format architecture with tiered processing | Accepted provisionally |
| ADR-006 | Uploads, URL capture and selected connectors | Accepted provisionally |
| ADR-007 | Hybrid source storage | Accepted provisionally |
| ADR-008 | Asynchronous ingestion | Accepted provisionally |
| ADR-009 | Immutable source versions | Accepted provisionally |
| ADR-010 | SHA-256 deduplication within security boundary | Accepted provisionally |
| ADR-011 | Tenant quotas and scheduled sync | Accepted provisionally |

## Data architecture

| ID | Decision | Status |
|---|---|---|
| ADR-012 | RushDB organization tenant | Accepted provisionally |
| ADR-013 | PostgreSQL authoritative metadata | Accepted provisionally |
| ADR-014 | Documents/chunks/entities/evidence-backed relationships | Accepted provisionally |
| ADR-015 | Eventual consistency with publication barrier | Accepted provisionally |
| ADR-016 | Immutable derived-knowledge revisions | Accepted provisionally |
| ADR-017 | Approved memory separate from source knowledge | Accepted provisionally |
| ADR-018 | Immediate denial and asynchronous physical purge | Accepted provisionally |

## AI architecture

| ID | Decision | Status |
|---|---|---|
| ADR-019 | Hybrid RAG + conditional GraphRAG | Accepted provisionally |
| ADR-020 | Provider-agnostic versioned embeddings | Accepted provisionally |
| ADR-021 | Composable services, optional LangGraph | Accepted provisionally |
| ADR-022 | Policy-constrained model gateway | Accepted provisionally |
| ADR-023 | Explicit approved durable memory | Accepted provisionally |
| ADR-024 | Grounded citations and abstention | Accepted provisionally |
| ADR-025 | Automated evaluation plus human review | Accepted provisionally |

## Security

| ID | Decision | Status |
|---|---|---|
| ADR-026 | Managed OIDC target, local OIDC for MVP | Accepted provisionally |
| ADR-027 | RBAC plus resource grants | Accepted provisionally |
| ADR-028 | Layered RLS/RushDB/R2 isolation | Accepted provisionally |
| ADR-029 | Server-side encrypted connector credentials | Accepted provisionally |
| ADR-030 | Tenant-owned AI data provider policies | Accepted provisionally |
| ADR-031 | Structured security audit events | Accepted provisionally |
| ADR-032 | Retention policies and verified deletion | Accepted provisionally |
| ADR-033 | Defense-in-depth against prompt injection | Accepted provisionally |

## Backend

| ID | Decision | Status |
|---|---|---|
| ADR-034 | FastAPI modular monolith | Accepted provisionally |
| ADR-035 | REST/OpenAPI and resumable SSE | Accepted provisionally |
| ADR-036 | Redis/Celery with PostgreSQL durable job state | Accepted provisionally |
| ADR-037 | Checkpointed stage pipeline | Accepted provisionally |
| ADR-038 | At-least-once idempotent execution | Accepted provisionally |
| ADR-039 | Incremental sync and webhooks | Accepted provisionally |
| ADR-040 | Separate bounded worker pools | Accepted provisionally |
| ADR-041 | Independently scale API/workers | Accepted provisionally |

## Frontend

| ID | Decision | Status |
|---|---|---|
| ADR-042 | Next.js App Router and TypeScript | Accepted provisionally |
| ADR-043 | Unified knowledge workspace | Accepted provisionally |
| ADR-044 | Organization → Workspace selection | Accepted provisionally |
| ADR-045 | Integrated source split-view | Accepted provisionally |
| ADR-046 | Contextual graph exploration | Accepted provisionally |
| ADR-047 | TanStack Query plus local UI state | Accepted provisionally |
| ADR-048 | SSE with persisted status recovery | Accepted provisionally |
| ADR-049 | Calm accessible research-focused design | Accepted provisionally |

## Infrastructure local-first

| ID | Decision | Status |
|---|---|---|
| ADR-050 | Docker Compose local-first | Accepted provisionally |
| ADR-051 | Local PostgreSQL/RushDB/Redis/S3-compatible store | Accepted provisionally |
| ADR-052 | Local development and isolated tests | Accepted provisionally |
| ADR-053 | GitHub Actions CI and local deployment scripts | Accepted provisionally |
| ADR-054 | OpenTelemetry and local monitoring | Accepted provisionally |
| ADR-055 | No recurring hosting cost; quotas | Accepted provisionally |
| ADR-056 | Local backups with restore validation | Accepted provisionally |
| ADR-057 | Feature flags, safe migrations and rollbacks | Accepted provisionally |
