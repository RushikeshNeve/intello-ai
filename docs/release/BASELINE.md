# Baseline M08 (`baseline-m08`)

A **baseline, not a release**: the state of the code after the M00-M08 functional build and lean hardening, before the M09 UI
revamp and M10 production deployment. No GitHub Release, no deployment, no public artifact.

## Version facts
| Item | Value |
|---|---|
| Product | Intelo AI (internal prefix `ukos`; rename is a planned task) |
| API / web package version | `ukos-api` 0.1.0, `ukos-web` 0.1.0 (exact dependency versions: `apps/api/uv.lock`, `apps/web/package-lock.json`) |
| Database migration head | `0010_memory_research` (`apps/api/migrations/versions/0010_memory_research.py`; chain 0001 -> 0010) |
| Embedding model / space | `google/embedding-gemma-300m`, 768 dimensions; embedding space id `eb3ad37f8d916` (derived from model, dimensions, prefixes, spec version 1; changing any of them is a new space and needs re-embedding) |
| Ingestion pipeline version | `1` (`apps/api/app/modules/ingestion/ids.py`, `PIPELINE_VERSION`) |
| Graph extractor | `graph-extract/1` |
| Chat model (local default) | `google/gemma-3-4b` via LM Studio 0.4.9 (`AI_BASE_URL`, OpenAI-compatible) |
| Infrastructure pins | PostgreSQL `postgres:17-alpine`, Redis `redis:7.4-alpine`, RushDB `rushdb/rushdb:3.0.10`, RustFS `rustfs/rustfs:1.0.5`, Dex `v2.45.1`, aws-cli `2.40.3` (all in `infrastructure/env/.env.example`) |
| Toolchain | Python 3.12, uv 0.11.x, Node 24, npm 11, Docker Engine 29.3.x with Compose v5 |
| OpenAPI contract | `apps/api/openapi.snapshot.json` (guarded by a test) |
| Evaluation baseline | `tests/evaluation/baseline.json` (offline golden set; gate `run_eval.py --require-baseline`) |

## Supported
Sign-in (OIDC, bundled Dex), organizations/workspaces/roles, upload + presigned storage, ingestion of text/Markdown/HTML/PDF/CSV/XLSX
with chunking and embeddings, hybrid search with permission-safe evidence, grounded streaming chat with citations and verification,
knowledge graph (entities, claims with evidence, bounded neighborhood, selective graph routing), connectors (GitHub source with
encrypted token, sync, revoke), safe URL capture, approved memory with evidence invalidation, bounded research sessions with cited reports
and Markdown export, deletion with purge, audit events, per-tenant isolation by PostgreSQL RLS. Everything runs locally on Docker Compose.

## Unsupported / out of scope at this baseline
- Production deployment, TLS, public hosting, multi-instance web sessions (in-memory session store) -> M10 (`REMAINING_PLAN.md`).
- A designed UI: the current UI is functional-but-plain; the full UI/UX revamp is M09 (`docs/ui-spec/`).
- Hosted inference providers as a default (LM Studio is local-development only; hosted use needs a permissive server and a re-embed); no cloud services.
- OCR, audio and video engines (only contracts and fakes), webhook/HMAC and non-GitHub connectors on a live stack, code-aware repository locators and XLSX not exercised live.
- Alias/coreference and cross-document graph reasoning, merge-review UI (`docs/execution/POST_M09_BACKLOG.md`).
- Windows-only developer host constraints (Application Control, Docker socket crash) are documented, not solved: `docs/runbooks/troubleshooting.md`.
- CI has not been executed on GitHub (validated locally with `scripts/ci/run_local.sh`); the nightly workflow is UNTESTED on GitHub.

## Known deferrals
- Deferred testing (thorough/adversarial/fuzz/real-browser passes): `docs/execution/TEST_DEBT.md`.
- Post-M09 improvements: `docs/execution/POST_M09_BACKLOG.md`.
- M09 (UI revamp) and M10 (production deployment, free-tier sources) scope: `docs/execution/REMAINING_PLAN.md`.
- Gate evidence and any gate not yet green: `docs/release/CHECKLIST.md`.

## Create the tag (coordinator, AFTER merging the M08 branches; local only)
```bash
git switch main
git tag -a baseline-m08 -m "Baseline M08: functional build M00-M08, lean hardening; migration head 0010_memory_research; embedding space eb3ad37f8d916; not a release"
git show baseline-m08 --stat | head -5
# push only with the milestone push (never auto): git push origin baseline-m08
```
Check the migration head before tagging: `ls apps/api/migrations/versions | tail -1` must still be `0010_memory_research.py` (update this
page if a later migration was merged). Rolling back to the baseline: `git switch --detach baseline-m08`.
