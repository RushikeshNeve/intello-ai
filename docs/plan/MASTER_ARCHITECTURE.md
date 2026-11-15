# Master System Architecture v1.0

## Goals
A multi-tenant Universal Knowledge Management System for individuals first, then teams and enterprises; initially local-first, with production-oriented module boundaries and no mandatory hosted services. Future hosted scale target: 100–1,000 users (not a local performance guarantee).

## Core stack
- Web: Next.js App Router, TypeScript, Tailwind, shadcn/ui, TanStack Query, React Flow (benchmark).
- API: FastAPI modular monolith, REST/OpenAPI, server-sent events.
- App system of record: PostgreSQL (organization, workspace, grants, documents, versions, jobs, usage, audit, publication).
- Knowledge representation: RushDB 3 candidate; one tenant/keyspace per organization, graph + vector + keyword/hybrid retrieval.
- File bytes: S3-compatible object store locally, interface switchable to Cloudflare R2.
- Worker delivery: Redis/Celery; PostgreSQL transactional outbox, leases, checkpoints and idempotent job stages.
- Identity: locally hosted OIDC provider candidate; managed OIDC for future SaaS.
- AI: provider-agnostic embedding registry and LLM gateway, local inference default, hybrid RAG + selectively invoked GraphRAG, approval-based long-term memory.
- Runtime: Docker Compose + local volumes; CI GitHub Actions within free limits; OpenTelemetry and light local dashboards.

## Ownership boundaries
| Entity | Authoritative system |
|---|---|
| Users, organizations, workspaces, roles, grants | PostgreSQL |
| Document IDs, versions, permissions, active publication pointer | PostgreSQL |
| Ingestion jobs, sync cursors, usage and audit | PostgreSQL |
| Document blobs, extracted immutable artifacts | S3-compatible object store |
| Search projections, vectors, entity/claim graph, evidence-linked memory | RushDB |

## Security invariants
1. Never trust tenant or workspace scope from a browser without backend verification.
2. Filter retrieval candidates by authorized scope before rank; revalidate candidates before LLM and source opening.
3. All graph edges that convey private facts carry evidence lineage and effective access constraints.
4. Deletion commits logical denial before asynchronous physical purge and backup expiration.
5. External inference, embeddings and transcription share the same tenant provider-policy enforcement.
6. Retrieved content cannot direct tool actions or change security policy.

## Ingestion state
Upload/fetch → validated source version → queued → extract → normalize → chunk → embed → optional graph enrich → index unpublished revision → validate → publication pointer swap. Retry stages independently; preserve old published revision on failure.

## Retrieval state
Authenticated question → scope resolution → BM25/vector search → optional evidence-constrained graph traversal → rank/deduplicate → revision/authorization validation → context assembly → model gateway → citation/claim checks → streaming answer.

## Release strategy
M00 validation gates must pass before the system depends on RushDB and local object-store implementations. M04 is the first complete usable Q&A vertical slice. M05–M08 add graph, connectors, research, and hardening. All tasks are proposed and unassigned until scheduled.

## Open risks
RushDB permission-safe multi-tenant retrieval/traversal and backup; local hardware capacity for generation/OCR; local S3 implementation license/maintainability; RLS bypass roles; cross-database deletion; connector permissions.
