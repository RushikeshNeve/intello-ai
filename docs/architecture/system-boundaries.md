# System Boundaries and ADR Index

Task: M00.01.01 · Baseline: Master Architecture v1.0 (2026-11-16) · Status of every ADR: **Accepted provisionally**

This document says which system owns each kind of data, who may read or write it, and which ADR justifies the
boundary. It is checked by `tests/docs/test_system_boundaries.py`: every `ADR-NNN` referenced here must exist in
[`docs/plan/ADR_REGISTER.md`](../plan/ADR_REGISTER.md), and the status column must match the register.

## 1. Runtime components

| Component | Technology | Role | Persistence | ADRs |
|---|---|---|---|---|
| Web | Next.js App Router, TypeScript, Tailwind, shadcn/ui | UI, session cookie holder, no direct DB access | none (browser cache only) | ADR-042, ADR-047, ADR-049 |
| API | FastAPI modular monolith | Only component that enforces authorization; owns REST/OpenAPI + SSE | none (stateless) | ADR-034, ADR-035, ADR-041 |
| Workers | Celery processes, separate bounded pools | Ingestion stages, purge, sync | none (stateless) | ADR-036, ADR-040 |
| PostgreSQL | PostgreSQL 17 | Authoritative operational DB, RLS | volume `postgres-data` | ADR-013, ADR-028 |
| RushDB | RushDB 3 (candidate, validated in M00.03) | Knowledge index: chunks, vectors, entities, evidence-backed edges | volume `rushdb-data` | ADR-012, ADR-014 |
| Object store | S3-compatible (provider chosen in M00.02.03) | Original bytes, immutable versions, artifacts | volume `objectstore-data` | ADR-007, ADR-051 |
| Redis | Redis 7 | Celery broker and cache only, never durable truth | ephemeral (AOF optional) | ADR-036 |
| Identity provider | Local OIDC (selected in M01.01.01) | User authentication | own volume | ADR-026 |
| Local inference | Ollama (benchmarked in M00.03.04) | Embeddings and generation behind the model gateway | model volume | ADR-020, ADR-022 |

## 2. Data ownership (authoritative system per entity)

| Entity | Authoritative system | Derived copies allowed in | Rule | ADRs |
|---|---|---|---|---|
| Users, organizations, workspaces, memberships, roles, grants | PostgreSQL | none (RushDB stores only opaque IDs needed for filtering) | RushDB never decides access | ADR-013, ADR-027 |
| Document IDs, versions, permissions, active publication pointer | PostgreSQL | RushDB (version ID and revision tag on every chunk) | A chunk is retrievable only if PostgreSQL says its revision is published | ADR-009, ADR-013, ADR-015 |
| Ingestion jobs, stage checkpoints, outbox, sync cursors | PostgreSQL | Redis (delivery messages only) | Redis loss must never lose work | ADR-036, ADR-037, ADR-038 |
| Usage, quotas, audit events | PostgreSQL | none | audit records are append-only and redacted | ADR-011, ADR-031, ADR-055 |
| Original document bytes, immutable versions | Object store | none | object keys contain the immutable version ID; objects are never overwritten | ADR-007, ADR-009, ADR-010 |
| Extracted artifacts (text, layout, OCR output) | Object store | RushDB (chunk text) | keyed by deterministic processing fingerprint | ADR-016, ADR-037 |
| Chunks, embeddings, entities, claims, relationships | RushDB | none | every record carries org, workspace, document, version and revision IDs | ADR-014, ADR-016, ADR-020 |
| Approved long-term memory | RushDB (records) + PostgreSQL (approval state) | none | unapproved suggestions are never durable facts | ADR-017, ADR-023 |
| Connector credentials | PostgreSQL (encrypted at rest) | none | never logged, never sent to the browser | ADR-029 |

## 3. Tenancy and isolation layers

| Layer | Mechanism | Protects against | ADRs |
|---|---|---|---|
| API | Backend-resolved org/workspace scope; RBAC + resource grants | Browser-supplied scope, privilege escalation | ADR-027, ADR-044 |
| PostgreSQL | Row-level security keyed on organization; app role is `NOBYPASSRLS` | Bugs that omit a WHERE clause | ADR-028 |
| RushDB | One fail-closed tenant (keyspace) per organization | Cross-organization reads/traversals | ADR-012, ADR-028 |
| RushDB (within an org) | Mandatory workspace + grant filters applied **before** ranking; revalidation before LLM context | Cross-workspace leakage inside one tenant. Org isolation does **not** protect workspaces. | ADR-019, ADR-028 |
| Object store | Per-object keys under `org/<org_id>/…`; access only through short-lived presigned URLs issued by the API | Direct bucket listing, URL guessing | ADR-028 |
| AI providers | Tenant provider policy enforced by the model gateway | Sending private data to a disallowed provider | ADR-022, ADR-030 |

## 4. Cross-system invariants

1. **Never trust browser scope.** The API derives the tenant and workspace from the authenticated principal and membership (ADR-027).
2. **Filter before rank.** Retrieval applies authorization filters inside the RushDB query, then revalidates candidates against PostgreSQL before they reach a model or a source viewer (ADR-019, ADR-024).
3. **Publication barrier.** Chunks for a revision are written unpublished; a PostgreSQL pointer swap publishes them atomically. Partial revisions are never served (ADR-015).
4. **Immutable versions.** Source versions and derived revisions are append-only; a new upload creates a new version (ADR-009, ADR-016).
5. **Deletion = denial first.** A delete commits a logical denial in PostgreSQL immediately; physical purge in RushDB/object store and backup expiry follow asynchronously (ADR-018, ADR-032).
6. **At-least-once workers.** Every job stage is idempotent and keyed by a deterministic ID; PostgreSQL holds the outbox and checkpoints (ADR-036, ADR-037, ADR-038).
7. **Evidence lineage.** Every private-fact graph edge cites evidence (chunk + version) and inherits its access constraints (ADR-014, ADR-019).
8. **Untrusted content.** Retrieved text cannot trigger tool actions or change policy (ADR-033).
9. **$0 local-first.** No mandatory paid hosting or inference; external providers are opt-in per tenant policy (ADR-050, ADR-055).

## 5. ADR index (from the register)

| ID | Decision | Status |
|---|---|---|
| ADR-001 | Phased individuals/teams/enterprise | Accepted provisionally |
| ADR-002 | Cloud SaaS product target | Accepted provisionally |
| ADR-003 | Initial 100–1,000-user hosted target | Accepted provisionally |
| ADR-004 | Shared multi-tenancy | Accepted provisionally |
| ADR-005 | Universal-format architecture with tiered processing | Accepted provisionally |
| ADR-006 | Uploads, URL capture and selected connectors | Accepted provisionally |
| ADR-007 | Hybrid source storage | Accepted provisionally |
| ADR-008 | Asynchronous ingestion | Accepted provisionally |
| ADR-009 | Immutable source versions | Accepted provisionally |
| ADR-010 | SHA-256 deduplication within security boundary | Accepted provisionally |
| ADR-011 | Tenant quotas and scheduled sync | Accepted provisionally |
| ADR-012 | RushDB organization tenant | Accepted provisionally |
| ADR-013 | PostgreSQL authoritative metadata | Accepted provisionally |
| ADR-014 | Documents/chunks/entities/evidence-backed relationships | Accepted provisionally |
| ADR-015 | Eventual consistency with publication barrier | Accepted provisionally |
| ADR-016 | Immutable derived-knowledge revisions | Accepted provisionally |
| ADR-017 | Approved memory separate from source knowledge | Accepted provisionally |
| ADR-018 | Immediate denial and asynchronous physical purge | Accepted provisionally |
| ADR-019 | Hybrid RAG + conditional GraphRAG | Accepted provisionally |
| ADR-020 | Provider-agnostic versioned embeddings | Accepted provisionally |
| ADR-021 | Composable services, optional LangGraph | Accepted provisionally |
| ADR-022 | Policy-constrained model gateway | Accepted provisionally |
| ADR-023 | Explicit approved durable memory | Accepted provisionally |
| ADR-024 | Grounded citations and abstention | Accepted provisionally |
| ADR-025 | Automated evaluation plus human review | Accepted provisionally |
| ADR-026 | Managed OIDC target, local OIDC for MVP | Accepted provisionally |
| ADR-027 | RBAC plus resource grants | Accepted provisionally |
| ADR-028 | Layered RLS/RushDB/R2 isolation | Accepted provisionally |
| ADR-029 | Server-side encrypted connector credentials | Accepted provisionally |
| ADR-030 | Tenant-owned AI data provider policies | Accepted provisionally |
| ADR-031 | Structured security audit events | Accepted provisionally |
| ADR-032 | Retention policies and verified deletion | Accepted provisionally |
| ADR-033 | Defense-in-depth against prompt injection | Accepted provisionally |
| ADR-034 | FastAPI modular monolith | Accepted provisionally |
| ADR-035 | REST/OpenAPI and resumable SSE | Accepted provisionally |
| ADR-036 | Redis/Celery with PostgreSQL durable job state | Accepted provisionally |
| ADR-037 | Checkpointed stage pipeline | Accepted provisionally |
| ADR-038 | At-least-once idempotent execution | Accepted provisionally |
| ADR-039 | Incremental sync and webhooks | Accepted provisionally |
| ADR-040 | Separate bounded worker pools | Accepted provisionally |
| ADR-041 | Independently scale API/workers | Accepted provisionally |
| ADR-042 | Next.js App Router and TypeScript | Accepted provisionally |
| ADR-043 | Unified knowledge workspace | Accepted provisionally |
| ADR-044 | Organization → Workspace selection | Accepted provisionally |
| ADR-045 | Integrated source split-view | Accepted provisionally |
| ADR-046 | Contextual graph exploration | Accepted provisionally |
| ADR-047 | TanStack Query plus local UI state | Accepted provisionally |
| ADR-048 | SSE with persisted status recovery | Accepted provisionally |
| ADR-049 | Calm accessible research-focused design | Accepted provisionally |
| ADR-050 | Docker Compose local-first | Accepted provisionally |
| ADR-051 | Local PostgreSQL/RushDB/Redis/S3-compatible store | Accepted provisionally |
| ADR-052 | Local development and isolated tests | Accepted provisionally |
| ADR-053 | GitHub Actions CI and local deployment scripts | Accepted provisionally |
| ADR-054 | OpenTelemetry and local monitoring | Accepted provisionally |
| ADR-055 | No recurring hosting cost; quotas | Accepted provisionally |
| ADR-056 | Local backups with restore validation | Accepted provisionally |
| ADR-057 | Feature flags, safe migrations and rollbacks | Accepted provisionally |

## 6. Provisional points pending M00 validation

- ADR-012/014 depend on the RushDB tenant, hybrid and graph spikes (M00.03.01–03). See `DECISION_CHANGES.md` DC-001.
- ADR-051's object store provider depends on M00.02.03 (DC-002).
- ADR-028 names R2 for future hosted object storage; locally the same S3 API is served by the chosen provider.
