# RushDB 3 validation (M00.02.02, M00.03.01–03)

Status: **CONDITIONAL GO** for ADR-012 (RushDB organization tenant) and ADR-014 (documents, chunks,
entities, evidence-backed relationships). The conditions are in §9.

## 1. Version tested

| Item | Value |
|---|---|
| Image | `rushdb/rushdb:3.0.10` (Docker Hub, official). Digest `sha256:95cc9ce5063e304e6c267718e0306f892bf23a95c5d09daada2b0ab08d2b7358`. On 2026-11-16 the tags `3`, `3.0` and `latest` pointed at the same digest. |
| Engine | `rushdb-lmpg`, Protocol v1. `/health` lists the languages `cypher, gremlin, mongo, searchQuery, sql, sqlPgq`. |
| Docs read | `rush-db/rushdb` main branch (CHANGELOG head 3.0.10, 2026-11-13): `docs/manual/**`, `deploy/templates/docker-compose/`, `Dockerfile`. |
| Release cadence | 3.0.0 shipped 2026-10-29 and 3.0.10 on 2026-11-13: eleven releases in about two weeks. The engine is very young (see risks). |
| Client | Raw HTTP through `httpx` (`tests/spikes/rushdb_support.py`). I did not use the PyPI `rushdb` SDK. The spikes need exact control of tenant headers and error envelopes, and the adapter should depend on the wire protocol, not on an SDK. |
| Host | Windows 11, Docker Desktop 29.3.1, Compose v5, Python 3.12, uv 0.11 |

## 2. Deployment (`infrastructure/compose/rushdb.yml`)

- Service `rushdb`, image `${RUSHDB_IMAGE:-rushdb/rushdb:3.0.10}`. The image is a single distroless Rust binary with entrypoint `rushdb serve`. It runs as uid 65532, and `RUSHDB_DATA_DIR=/data` is baked into the image.
- The named volume `rushdb-data` is mounted at `/data`. The directory holds `config.yml`, `rushdb.redb`, the `rushdb.redb.wal/` segments, `text/<tenant>/…` (tantivy) and `vector-segments/<tenant>/…`.
- Command: `--multitenant --pgwire-port 0 --languages sql,gremlin,sql-pgq,mongodb,cypher`.
  - `--multitenant` is a CLI flag only (there is no env var). The mode is written to `config.yml` on first start, and the server refuses to open the directory in the other mode. **A volume created single-tenant cannot be reused.**
  - pgwire is disabled on purpose. The API talks HTTP only. RushDB also *refuses* to bind pgwire/gRPC/Bolt on a non-loopback listener in multitenant mode, and the container listens on `0.0.0.0`. Publishing pgwire would need TLS plus `--capability-secret`. I do not recommend it: SQL is available over HTTP through `query.statement`.
  - Cypher is enabled over HTTP (no Bolt) because graph statements were part of the evaluation. It is optional: every pattern the adapter needs also works in the native JSON query.
- Token: `RUSHDB_TOKEN`. The container refuses the default `local-token`.
- Port: `${UKOS_BIND_HOST:-127.0.0.1}:${RUSHDB_HTTP_HOST_PORT:-3310}:3000`.
- Healthcheck: `["CMD","/usr/local/bin/rushdb","health"]`. The image has no shell or curl. `GET /ready` returns `commitIndex`.
- `stop_grace_period: 90s`, taken from the upstream template: a clean stop drains requests and writes a checkpoint.
- No `version:` key, no networks and no restart policy.
- Standalone start: `docker compose -p ukos-rushdb --env-file infrastructure/env/.env.example -f infrastructure/compose/rushdb.yml up -d --wait` brings the service up and healthy in about 5 s.

## 3. Exact API patterns

All requests are `POST /protocol/v1/execute` with these headers:

```
Authorization: Bearer $RUSHDB_TOKEN
X-RushDB-Tenant: <org scope>
Content-Type: application/json
```

The body is `{"protocolVersion":1,"context":{"scope":"<org scope>"},"operation":{…}}`. The header scope and `context.scope` must be equal. The `operation` objects below are what the spikes send.

### 3.1 Tenant scoping

| Case | Result (observed) |
|---|---|
| No header and no scope | 403 `missing_scope` |
| Header, no `context.scope` | 403 `missing_scope` |
| `context.scope`, no header | 403 `missing_scope` |
| Header differs from `context.scope` | 403 `scope_mismatch` |
| Wrong token, or no token | 401 `{"error":"unauthorized"}` |
| **Unknown scope** | **200, empty.** The first request registers a fresh, empty keyspace. Tenants are created implicitly; there is no "create tenant" call. |
| Scope differing only in case | A different, empty tenant (scopes are byte-exact) |
| Purge, then reuse the scope | A fresh, empty keyspace (ordinals are never reused) |
| Erase a tenant | `{"kind":"purgeTenant"}`. This also removes the tenant's `text/<ordinal>/` directory (verified). |

**One operator token addresses every tenant** (verified). The tenant is chosen by the header, not by the token. See invariant S1 in §9.

### 3.2 Writes

```jsonc
// create (explicit id is optional; ids are per-tenant: the same UUID can exist in two tenants)
{"kind":"mutate","mutation":{"kind":"putRecord","mode":"create","label":"Chunk","id":"<uuid?>",
  "properties":{…},"options":{},
  "vectors":[{"propertyName":"embedding","vector":[…],"index":"chunk-emb"}]}}
// bulk (nested docs, inline vectors)
{"kind":"ingest","ingest":{"kind":"documents","label":"Chunk","documents":[{…,"$vectors":[{"propertyName":"embedding","vector":[…],"index":"chunk-emb"}]}],"options":{}}}
// relationships (skips existing; atomic; not_found if any id is unknown in THIS tenant)
{"kind":"mutate","mutation":{"kind":"linkRecordsById","command":{"relationType":"HAS_CHUNK","direction":"out","sourceIds":[…],"targetIds":[…],"properties":{"workspace_id":"w1"}}}}
// idempotent upsert by id
{"kind":"mutate","mutation":{"kind":"putRecord","mode":"merge","id":"<uuid>","label":"Chunk","properties":{…}}}
```

Gotcha: `options.suggestTypes:false` stores **every value as a string** (`n: 0` becomes `"0"`), and type-strict filters then match nothing. Keep the default `options: {}`. Booleans and numbers then stay typed. ISO date strings become datetimes.

### 3.3 Indexes

```jsonc
{"kind":"index","index":{"kind":"createVector","definition":{"name":"chunk-emb","label":"Chunk","property":"embedding","dimensions":8,"similarity":"cosine","model":"<embedding model id>"}}}
{"kind":"index","index":{"kind":"upsertVectors","name":"chunk-emb","items":[{"recordId":"<id>","vector":[…]}]}}
{"kind":"index","index":{"kind":"create","definition":{"name":"chunk-text","label":"Chunk","property":"text","kind":"text","consistency":"immediate"}}}
{"kind":"index","index":{"kind":"drop","name":"chunk-text"}}
{"kind":"describe","describe":{"kind":"indexes"}}          // text/ordered: field is "state" ("building"|"ready")
{"kind":"describe","describe":{"kind":"vectorIndex","name":"chunk-emb"}}  // stats.indexedRecords, annLifecycle
```

- A vector index is immutable (dims and metric). Re-declaring it with a different `model` is a `conflict`. Use one index per embedding model/version (ADR-020); the limit is 256 per tenant.
- A text index starts in `building`. The adapter must poll until `state=="ready"`; a search before that fails as `unavailable`.

### 3.4 BM25

```jsonc
{"kind":"query","query":{"kind":"text","index":"chunk-text","query":"tide tables harbor","limit":6}}
// -> {"kind":"textHits","rows":[{"id":…,"score":…}],"pending":0}
```

`query.text` takes **no predicate, no labels and no offset**. It is tenant-scoped but **not workspace-filterable**. Filtered BM25 is a hybrid query with only a text leg:

```jsonc
{"kind":"query","query":{"kind":"hybrid","query":{"labels":["Chunk"],"limit":4,
  "predicate":{"workspace_id":"ws-alpha","revision_id":{"$in":["light-r1","tides-r1"]}},
  "text":{"index":"chunk-text","query":"lighthouse keeper"}}}}
```

Scoring is tantivy BM25 (k1=1.2, b=0.75, idf=ln(1+(N−df+0.5)/(df+0.5))). Tokens are split on non-alphanumerics and lower-cased, with no stemming or stop words. Corpus statistics cover the whole index, so every workspace in the tenant feeds the IDF. Scores matched my reference implementation to 1e-4.

### 3.5 Vector

```jsonc
{"kind":"query","query":{"kind":"vector","query":{"labels":["Chunk"],"property":"embedding","index":"chunk-emb",
  "vector":[…],"limit":6,"mode":"exact|auto|ann","ef":64,"predicate":{…}}}}
// -> records with "score"; "vectorSearch":{"mode","pool","fallback"?}
```

- The predicate is resolved to a candidate set *before* ranking, in both exact and HNSW modes.
- `auto` searches exactly up to a pool of 5,000 and uses HNSW above that.
- Even a forced `ann` is answered exactly (`fallback: annWarming`) while the graph builds in the background. The build took about 0.8 s for 12k 8-d vectors.

### 3.6 Hybrid (native fusion)

```jsonc
{"kind":"query","query":{"kind":"hybrid","query":{"labels":["Chunk"],"limit":6,"predicate":{…},
  "vector":{"property":"embedding","vector":[…],"index":"chunk-emb"},
  "text":{"index":"chunk-text","query":"lamp beam harbor"},
  "fusion":"rrf","rrfK":60}}}
// records carry score, vectorScore, textScore, provenance ["vector:0","lexical:2"]; response has "fusion"
```

- Fusion is native. The default is RRF: `weight/(rrfK+rank+1)`, rank from 0, with each leg prefetching `4×(offset+limit)`. `dbsf` (distribution-based score fusion) is also available.
- Fused scores matched a client-side RRF reference to 1e-12, so the app does not need its own RRF.
- Hybrid covers records only, not relationship vectors.

### 3.7 Filters and traversal

```jsonc
// property filter + workspace + published revisions
{"labels":["Chunk"],"predicate":{"workspace_id":"w1","revision_id":{"$in":[…]}}}
// by id with workspace (query.record has NO predicate)
{"labels":[],"predicate":{"$and":[{"workspace_id":"w1"},{"$id":"<id>"}]}}
// relationship block: label key = related record's label; $relation restricts type/direction/hops
{"labels":["Chunk"],"predicate":{"$and":[{"workspace_id":"w1"},
  {"Entity":{"$relation":{"type":"MENTIONS","direction":"out"},"workspace_id":"w1"}}]}}
// relationship listing filters on RELATIONSHIP properties only
{"kind":"query","query":{"kind":"relationships","query":{"relationType":"HAS_CHUNK","predicate":{"workspace_id":"w1"}}}}
// counts
{"labels":["Chunk"],"predicate":{…},"select":{"n":{"$count":"*"}}}
// Cypher over HTTP (parameterized)
{"kind":"query","query":{"kind":"statement","language":"cypher","statement":"MATCH (c:Chunk) WHERE c.workspace_id = $ws RETURN count(c) AS n","parameters":{"ws":"w1"}}}
```

A filter on a related block constrains only that block. **The root label needs its own workspace predicate.**

### 3.8 Evidence-linked traversal (single native query)

```jsonc
{"labels":["Claim"],
 "predicate":{"$and":[
   {"Entity":{"$relation":{"direction":"out"},"$id":"<entity id>"}},
   {"Evidence":{"$alias":"$ev","$relation":{"type":"SUPPORTED_BY","direction":"out"},
     "Chunk":{"$relation":{"type":"FROM_CHUNK","direction":"out"},
              "workspace_id":{"$in":["w1"]},"revision_id":{"$in":[…published…]}}}}]},
 "select":{"evidence":{"$collect":{"from":"$ev","select":{"key":"$ev.key"},"orderBy":{"key":"asc"}}}},
 "limit":100}
```

This returns only claims that have at least one authorized, published evidence, and each claim carries **only** that evidence (`$collect` follows the alias's full filtered path). The Cypher equivalent gives the same result:

```
MATCH (e:Entity {name:$name})<-[:SUBJECT|OBJECT]-(c:Claim)-[:SUPPORTED_BY]->(ev:Evidence)-[:FROM_CHUNK]->(ch:Chunk)
WHERE ch.workspace_id IN $ws AND ch.revision_id IN $revs RETURN c.key, ch.key
```

RushDB relationships cannot point at records, so an entity-to-entity fact is **reified** as a `Claim` record. Edges keep their own properties; that is enough for HAS_CHUNK and MENTIONS, but not for multi-evidence facts.

### 3.9 Deletion

```jsonc
{"kind":"mutate","mutation":{"kind":"deleteRecords","query":{"labels":["Evidence"],"predicate":{"version_id":"doc-1-v1"}}}}
```

Purge procedure for a deleted document version (app-driven, idempotent, one `deleteRecords` per step):

1. Delete `Evidence` records with `{version_id: V}`.
2. Delete `Chunk` records with `{version_id: V}`. Their relationships go with them.
3. Delete `Claim` records with `{"$not":{"Evidence":{"$relation":{"type":"SUPPORTED_BY"}}}}`.
4. Delete `Entity` records with `{"$not":{"Claim":{}}}`.

`deleteRecords` commits in pages of 500 and is **not one transaction**. The PostgreSQL denial (unpublishing the revision) must therefore come first; the query-time revision filter hides the data until the purge finishes. Deleting chunks alone already hides v1-only edges, but it leaves orphan Evidence and Claim records. The full procedure leaves no orphans (verified).

### 3.10 Backup

```bash
# online physical base (operator token; written under /data/backups/<out>)
curl -X POST -H "Authorization: Bearer $RUSHDB_TOKEN" "http://127.0.0.1:3310/internal/backup/base?out=base-1"
docker exec <c> /usr/local/bin/rushdb serve --verify-base /data/backups/base-1   # -> verify-base: OK
```

## 4. Results per acceptance criterion

| Task | Acceptance | Verdict | Evidence |
|---|---|---|---|
| M00.02.02 | Restart preserves graph and vector records | **PASS** | `rushdb_persistence.py`: records, Document→Chunk and Chunk→Entity traversal, vector top-3 with scores, BM25 hits, relationship list and `indexedRecords` were identical after `restart`, and again after `down` (volume kept) + `up` on a new container id. |
| M00.03.01 | Cross-tenant read and traversal tests deny leakage | **PASS**, with conditions S1/S2 | `rushdb_tenants.py`, 30 tests. From tenant B, **none** of A's data was visible through get-by-id (including an id that exists in both tenants: B gets B's record), `$id` filter, label listing and catalog, property filters, BM25, vector (exact and ANN), hybrid, relationship blocks (1 and 2 hops), relationship listing, `$count`/groupBy, SQL (A's labels are not even tables in B), Cypher and Gremlin. Writes from B (delete-by-query, merge on the shared id, link to an A id) cannot touch A. Fail-closed matrix as in §3.1. Inside one tenant, unfiltered queries **do** return both workspaces. A mandatory workspace predicate excludes the other workspace for records, get-by-id (through `$id`), counts, vector (exact/auto/ann), hybrid, filtered BM25 (through hybrid), traversal (root + block), relationship listing (edge properties) and Cypher. **`query.text` cannot be filtered.** |
| M00.03.02 | BM25, vector and filtered fusion return expected fixtures | **PASS** | `rushdb_hybrid.py`, 13 tests, on an original 30-chunk corpus with hand-made 8-d vectors. All six expected orderings match exactly, and BM25, cosine and RRF scores match reference implementations. Filter-before-rank: the unauthorized and unpublished chunks rank highest unfiltered, yet the filtered pages were full (k=4 BM25, k=6 vector, k=6 hybrid) and never unauthorized. Unpublished-revision chunks never appear under the filter, while the same query without the filter surfaces them. Filtered HNSW at scale (6,000 authorized + 6,000 closer unauthorized vectors, `mode=ann`, pool 6,000): 10/10 authorized, recall@10 = 1.00 against exact. |
| M00.03.03 | Evidence-linked traversal and deletion tests pass | **PASS** | `rushdb_graph.py`, 8 tests. Traversal returns only claims with authorized evidence: an all-unauthorized claim is excluded and a mixed claim keeps only its authorized evidence. Subject and object directions both work, as does the other-workspace view. Unpublishing a revision hides its evidence immediately. The version purge removes v1-only claims (C1, C5), keeps C2 with reduced evidence, leaves no orphan Evidence/Claim/relationship, is idempotent, and cleans up an entity whose last claim disappears. **This takes a single native query (or a single Cypher query); no app-side evidence filtering is needed.** |

## 5. Latency (client round trip, localhost, Docker Desktop on Windows; median / p95 of 20 runs)

| Query | Median ms | p95 ms |
|---|---|---|
| BM25 `query.text`, 30 chunks | 1.7 | 2.4 |
| BM25 filtered (hybrid text leg) | 2.0 | 2.2 |
| Vector exact, unfiltered | 2.1 | 2.4 |
| Vector filtered (exact / auto / ann→exact fallback) | 2.0–2.1 | 2.6–3.0 |
| Hybrid RRF filtered | 2.4 | 3.3 |
| Vector HNSW filtered, 6k of 12k pool (`mode=ann`, ef=64) | 7.2 | 8.8 |
| Vector exact filtered, 6k of 12k pool | 7.4 | 8.4 |
| Hybrid vector leg filtered, 6k of 12k | 7.2 | 8.8 |
| Ingest 12,000 records with 8-d vectors (12 × 1,000) | 836 total | — |
| HNSW warm-up (first `ann` call until `mode=ann`) | ~800 | — |

These are toy-scale numbers. They show that nothing is pathological, not production capacity. Realistic vectors (384–1024 dims) and 10⁵–10⁶ chunks still need a benchmark (M04).

## 6. Backup and restore (input to M00.03.05)

I tested two methods against a fixture tenant: a Document plus 5 chunks with vectors, a text index and HAS_CHUNK links. The check is a SHA-256 over records + traversal + vector top-5 scores + BM25 hits.

1. **Cold volume snapshot (recommended for local/M00.03.05; tested; exact).**

   ```bash
   docker compose -p ukos-rushdb … stop rushdb                     # clean checkpoint (~1 s)
   docker cp <container>:/data - > rushdb-data.tar                  # tar stream keeps uid 65532
   docker volume create <fresh>; docker create --name <check> -v <fresh>:/data … rushdb/rushdb:3.0.10 --multitenant --pgwire-port 0 …
   docker cp -a - <check>:/ < rushdb-data.tar; docker start <check>
   ```

   The restored node came up `ready` at the same `commitIndex`, and the checksum was **identical** (`4bdcad30…`): records, traversal, vector scores and BM25 hits. This needs no helper image. Downtime equals the copy time.

2. **Online base backup (`POST /internal/backup/base`).** The call worked (`{"commitIndex":956,"files":3}`), and `--verify-base` returned OK. **But** the base holds only the store, the layout and `config.yml`, with no `text/` directories. A node started on that base reported the text index `ready` with `pending: 0`, yet **returned zero BM25 hits**. Records, traversal and vectors were correct. Dropping and re-creating the text index restored identical results.

   Caveat: I started the node directly on `base/`, not through the documented `--restore-pitr` path, which needs a WAL archive. That path is untested here. Either way, after any restore the runbook must re-create text indexes, or at least run a BM25 smoke query.

The graph snapshot (`--export/--restore`) and the protocol `backup` op do not include vectors or indexes, so they are not suitable as primary backups.

The whole-node backup contains **every tenant**. Per-tenant restore is not supported for physical backups.

## 7. What the M03.03.01 adapter must do

1. Derive the org scope server-side from the authenticated principal. Send it as **both** `X-RushDB-Tenant` and `context.scope`. Never accept a scope from the client. Validate that the org exists in PostgreSQL before calling RushDB, because unknown scopes are silently auto-registered.
2. Add the mandatory predicate `{"workspace_id": {"$in": <authorized ws>}, "revision_id": {"$in": <published revisions>}}` to every read, built in one place (no call site composes its own). In traversals, apply it to the root **and** to every block that reaches a scoped record.
3. Never use `query.text` for user retrieval; use `query.hybrid` with a text leg. Never use `query.record`; use `query.records` + `$id` + the predicate. Do not expose `query.statement` (SQL/Cypher/Gremlin) to user input. If Cypher is used internally, it must be parameterized and carry the same predicate.
4. Copy scope ids onto every record **and** every relationship: org_id, workspace_id, document_id, version_id, revision_id. Relationship listing can only filter on edge properties.
5. Reify facts as Claim → Evidence → Chunk (§3.8). Evidence copies the chunk's scope ids so the purge works without a traversal.
6. Use one vector index per embedding model/version (`model` field) and poll text indexes until ready. Use explicit ids plus `merge` for idempotent retries (`create` with an existing id is `conflict`).
7. Deletion: deny in PostgreSQL first, then run the purge procedure in §3.9 as an idempotent worker stage. After a restore, re-create text indexes.
8. Keep revalidating candidates against PostgreSQL before they go to the LLM (invariant 2). RushDB filtering is defense in depth, not the authority.

## 8. Gaps and risks

- **R1 – Shared operator token (high).** Any holder of `RUSHDB_TOKEN` can read or write any tenant and create tenants by naming them. Isolation is only as strong as the API's scope derivation. Mitigation available in the engine but not tested here: signed per-tenant capability tokens (`--capability-secret`; a mismatched tenant returns 401).
- **R2 – Workspace isolation is app-enforced (high, by design).** RushDB has optional in-tenant data-access policies (label grants, property visibility), but they cannot express a "workspace_id IN" row filter. Under an enforced policy, `query.text` and statements are refused. Every query must carry the workspace predicate, so the adapter needs a single choke point and adversarial tests (M08).
- **R3 – `query.text` cannot be filtered** (see §3.4). Mitigated by hybrid with a text-only leg.
- **R4 – Engine maturity (medium–high).** 3.0.0 is under 3 weeks old with near-daily releases. openCypher and GQL are "release candidate". The docs deviated from behaviour in one place: forced `ann` still falls back while the graph warms. Pin an exact tag and re-run these spikes on every upgrade.
- **R5 – Backup completeness (medium).** An online base did not carry text index content (§6). A cold snapshot needs a stop. Physical backups are whole-node, with no per-tenant restore.
- **R6 – Non-transactional purge (low).** `deleteRecords` and `purgeTenant` run as bounded commits, so the PostgreSQL denial must come first. Deleted text and MVCC history stay on disk until background compaction or segment merge. Verified-deletion evidence (ADR-032) needs a check after compaction (not tested).
- **R7 – Scale untested.** All measurements used ≤12k records with 8-d vectors. Memory budgets to watch: the ANN graph (default 128 MiB logical cache), 256 vector spaces per tenant, and the 16 MiB body limit.
- **R8 – Single writer per node.** Commits from all tenants are serialized. Ingest must batch.

## 9. Recommendation: **CONDITIONAL GO** for ADR-012 / ADR-014

RushDB 3.0.10 met every acceptance criterion with real, passing tests. Org isolation is structural (keyspace prefix): no leak on any surface tried. Filter-before-rank works for vector, ANN, BM25 (through hybrid) and native RRF/DBSF fusion. Evidence-scoped traversal is a single query. Persistence and a cold restore are exact.

Conditions:

1. **S1.** Only the API (and the ingestion/purge workers that strictly need it) holds `RUSHDB_TOKEN`. It is never sent to the browser or logged. The scope comes from the authenticated principal, and the org's existence is validated against PostgreSQL. Evaluate capability tokens in M01/M08.
2. **S2.** All retrieval goes through one adapter that injects the workspace + published-revision predicate, follows the rules in §7, and has adversarial cross-workspace tests in CI.
3. Pin `rushdb/rushdb:3.0.10`. Upgrades require re-running `tests/spikes/rushdb_*.py`.
4. M00.03.05 uses the cold volume snapshot (or base backup + text index re-creation), with a BM25 check in the restore smoke test.
5. M04 benchmarks realistic dimensions and volumes before beta. If p95 or memory is unacceptable, revisit the decision.

Reproduce:

```bash
docker compose -p ukos-rushdb --env-file infrastructure/env/.env.example -f infrastructure/compose/rushdb.yml up -d --wait
uv run --no-project --with pytest --with httpx python -m pytest -q tests/spikes/rushdb_persistence.py tests/spikes/rushdb_tenants.py tests/spikes/rushdb_hybrid.py tests/spikes/rushdb_graph.py
docker compose -p ukos-rushdb --env-file infrastructure/env/.env.example -f infrastructure/compose/rushdb.yml down -v
```

On this machine an Application Control policy blocks the `pytest.exe` launcher, so use `python -m pytest`.
