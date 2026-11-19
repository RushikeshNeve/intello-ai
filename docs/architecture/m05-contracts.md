# M05 contracts: knowledge graph and selective GraphRAG (slim)

Frozen 2026-11-19. Builds on m03/m04 contracts. Lean mode: sanity tests only (see `docs/execution/TEST_DEBT.md`).

## 1. Principles
1. **PostgreSQL is authority for identity and review; RushDB holds the graph projection** (like chunks). Everything in RushDB is rebuildable from artifacts + PG.
2. **Graph data never crosses workspaces.** Entities, claims and edges are created per workspace and carry `org_id`/`workspace_id`; entity resolution merges only inside one workspace. This removes cross-workspace leakage by construction.
3. **Every claim/edge has evidence lineage**: `evidence` = (chunk_id, revision_id, document_id, version_id). A claim with no resolvable evidence is stored `unverified` and is **never** returned by retrieval/traversal (ADR-014/024).
4. **Lineage lifecycle follows the revision**: graph records of a revision get `published=true` only while that revision is the published one; supersede/delete/purge removes or unflags them (extend the M03 reconcile + deletion handlers). Retrieval revalidates evidence against PostgreSQL (`is_revision_published`) before use.
5. Extraction is **local-LLM by default** through the M04 gateway (`get_chat_model`), JSON-constrained, per chunk batch, deterministic temperature 0, bounded cost (`GRAPH_MAX_CHUNKS_PER_REVISION`, `GRAPH_MAX_ENTITIES_PER_CHUNK`). Retrieved text is untrusted: extraction output is schema-validated and cannot create types/predicates outside the ontology.
6. Graph build is **asynchronous and optional**: triggered by the existing outbox event `knowledge.revision.published` (Celery `maintenance` queue task `graph.extract_revision(org_id, document_id, revision_id)`), never blocks publication; failure leaves the document fully searchable.

## 2. Ontology (M05.01.01) — `app/modules/graph/ontology.py`
- Entity types (closed): `person, organization, project, technology, concept, location, event, document_ref, other`.
- Predicates (closed): `works_for, member_of, owns, depends_on, uses, part_of, located_in, related_to, authored, mentions, supersedes, caused_by`.
- RushDB labels: `Entity {id, org_id, workspace_id, type, canonical_name, aliases[], entity_state}`, `Claim {id, org_id, workspace_id, subject_id, predicate, object_id|object_text, confidence, verification: verified|unverified, revision_id, published}`, `Evidence {id, claim_id, chunk_id, revision_id, document_id, version_id, excerpt}`; relationships `(Entity)-[:SUBJECT_OF]->(Claim)`, `(Claim)-[:OBJECT]->(Entity)`, `(Claim)-[:SUPPORTED_BY]->(Evidence)`. Deterministic ids (uuid5 over org/workspace/type/normalized name for entities; over subject/predicate/object/revision for claims; evidence over claim+chunk) so re-extraction is idempotent (merge writes).
- Provenance fields on every record: `extractor_version`, `source_revision_id`, `created_at`.

## 3. Extraction, resolution, claims (M05.01.02–.04) — `app/modules/graph/`
- `extract.py`: prompt (versioned `GRAPH_PROMPT_VERSION`), strict JSON schema validation, drops items violating ontology/limits, records source chunk ids and version ids on each item. Marker for chunk-level hostile text: the fenced evidence format of `ai/context.py` is reused.
- `resolve.py`: per-workspace canonicalization: normalize name (NFC, casefold, punctuation/whitespace), exact-match merge; fuzzy candidates (token Jaccard ≥ 0.8 or edit distance ≤ 2 for same type) become **merge candidates, not merges**. Migration `0008_graph`: `graph_entities(id, org_id, workspace_id, type, canonical_name, aliases jsonb, state active|merged|rejected, merged_into, created_at)` and `entity_merge_candidates(id, org_id, workspace_id, entity_a, entity_b, score, state pending|accepted|rejected, decided_by, decided_at)`; RLS like other org tables, workspace-checked, no DELETE; routes `GET /v1/orgs/{o}/workspaces/{w}/graph/merge-candidates` and `POST …/{id}/decision` (workspace editor+, audited). Accepted merge rewrites canonical id mapping (alias table), conflicting identities (same name, different type) stay separate and listed.
- `claims.py`: writes Claim+Evidence; `verification=verified` only if every evidence chunk exists in the artifact store for that revision and the claim's surface text overlaps it (reuse `ai/verification` lexical support); else `unverified` (never returned, shown only in admin diagnostics).
- Task `graph.extract_revision`: idempotent, fenced by checking the revision is still the published pointer at write time; reconcile/delete handlers extended to remove graph records for superseded/purged revisions.

## 4. Traversal, routing, merge (M05.02) — `app/modules/graph/{traversal,router,merge}.py`
- `traverse(scope, repo, seed_entity_ids|query, *, depth≤2, max_nodes=50, max_edges=100)`: every hop's predicate includes `workspace_id IN scope AND published = true AND verification='verified'`; returns paths with evidence refs; hard caps; cycle-safe; no counts of hidden items leaked (counts only over authorized).
- `route(question) -> basic | graph`: cheap heuristic (relationship/multi-hop cues: "who works with", "related to", "depends on", "how are X and Y", multiple named entities present in the workspace graph) + entity linking over the question; **basic questions never invoke the graph** (assert in tests). Settings `GRAPH_ROUTER_MODE=auto|off|force`.
- `merge.py`: graph paths → extra `Evidence` items (the supporting original chunks, revalidated through `retrieval.revalidate`), tagged `via_graph=true` with a short path description; the chat runtime/prompt consumes them like normal evidence; **citations always point at original chunks**, never at graph nodes.
- Chat integration: `chat_service`/runtime calls the router; behind `GRAPH_ENABLED` (default false until the gate passes; the M05 gate turns it on in the dev compose env).

## 5. Graph API and UI (M05.03)
- `GET /v1/orgs/{o}/workspaces/{w}/graph/neighborhood?entity_id=&depth=1..2&limit=1..100&cursor=` → `{nodes:[{id,type,name,degree_visible}], edges:[{id,predicate,subject_id,object_id,claim_id,confidence,evidence_count}], truncated, next_cursor}`; `GET …/graph/entities?q=&type=&limit=` (search/list, keyset); `GET …/graph/claims/{claim_id}` → `{claim, evidence:[{chunk_id,document_id,version_id,title,page_start,page_end,excerpt}]}` (evidence revalidated; document_id/version_id link to the M04 evidence viewer). All `document.read`-level on the workspace, concealed 404, node/edge caps enforced server-side.
- Web: React Flow (dependency allowed: `@xyflow/react`) contextual explorer (search entity → neighborhood; expand node; keyboard: arrow keys move focus between nodes, Enter expands, Esc collapses, edge list as accessible table alternative), edge click opens a provenance panel with citations → evidence viewer.

## 6. Benchmark (M05.03.04)
`tests/evaluation/graph_benchmark.py`: extend the golden set with ~20 relationship/multi-hop questions over the seed corpus; run hybrid-only vs hybrid+graph with the scripted/fake model (and optional live), report recall, answer-support, latency delta in `reports/graph_benchmark.json`. Quality claim is reported, not gated.

## 7. Settings
`GRAPH_ENABLED=false`, `GRAPH_ROUTER_MODE=auto`, `GRAPH_MAX_CHUNKS_PER_REVISION=400`, `GRAPH_MAX_ENTITIES_PER_CHUNK=12`, `GRAPH_EXTRACT_BATCH_CHUNKS=6`, `GRAPH_TRAVERSAL_MAX_DEPTH=2`, `GRAPH_MAX_NODES=50`.

## 8. Lean test list (per task)
Happy path; one cross-workspace and one cross-org check per new route/table/traversal; unsupported claim never returned; basic question does not touch the graph; deletion/supersede removes graph records; caps enforced.
