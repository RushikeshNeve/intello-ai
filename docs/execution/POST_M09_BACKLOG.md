# Post-M09 improvements (user-requested backlog; not part of M00–M09)

## Graph: relationships across documents (requested 2026-11-20)
**Today (M05):** entities are workspace-scoped with deterministic ids (type + normalized name), so the SAME entity name in two documents of one workspace is one node with edges from both. Fuzzy near-matches become *merge candidates* (API only, no UI). There are no document-to-document edges, no cross-workspace links (by design), and no cross-document inference.

| # | Improvement | Notes |
|---|---|---|
| G1 | **Alias / coreference resolution** ("Orion" ≈ "Orion Systems", acronyms, nicknames) | per-workspace alias table; batch LLM-assisted resolver proposing merges; auto-accept above a confidence threshold, review below |
| G2 | **Type-tolerant matching** | match names across compatible types (project/technology/concept) with review; stop type disagreement splitting one real entity |
| G3 | **Merge-review UI** | screen for `GET/POST …/graph/merge-candidates` (API exists): side-by-side evidence, accept/reject, undo |
| G4 | **Document-to-document edges** | `Document` nodes + edges `references` (explicit mentions/links/filenames), `supersedes`/`updates` (versions across docs), `related_to` (shared-entity overlap ≥ k, embedding similarity); evidence lineage on both sides; "Related documents" panel in the viewer and a document layer in the explorer |
| G5 | **Cross-document claim reasoning** | corroboration (claim supported by N documents), contradiction detection (same subject+predicate, different object), recency (newer document supersedes), shown with provenance |
| G6 | **Better multi-hop GraphRAG over linked documents** | path ranking, document-level hops, router tuning; extend the hybrid-vs-graph benchmark with cross-document questions |
| G7 | **Cross-workspace linking (opt-in)** | needs an ADR: org-level shared entity layer with per-edge access constraints; never leaks claims/counts from workspaces the user cannot read; default OFF |
| G8 | **Extraction quality** | stronger/optional extractor model (local larger model or OpenAI under tenant policy), prompt-version re-extraction jobs, incremental updates, per-document extraction diagnostics |

Order of value/effort: G3 → G1/G2 → G4 → G5 → G6 → G8 → G7.

## Product/UI items deferred out of M09 (2026-11-20)
- **Invitations** (invite by email; today member PUT needs an existing user_id): needs backend (invitation tokens, email delivery or link, accept flow, expiry, audit). M09 ships the invite dialog as a disabled/honest state.
- **Usage metering** endpoint (`GET /orgs/{o}/usage`): needs a metering source (tokens, storage, runs); M09 shows a disabled Usage panel.
- Chat feedback, notifications, "related concepts" chips: no backend; not planned.
