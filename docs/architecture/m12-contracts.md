# M12 contracts

## M12.05
Re-embedding / index migration (migration `0018_reindex`, `modules/knowledge/reindex/`, `scripts/ops/reindex.sh`).
- PG is the authority: `reindex_jobs` (state planned|running|verifying|ready|cutover|rolled_back|failed, counts, lease + `lease_epoch` fencing) and `org_active_spaces` (active-space pointer; absent = configured default). One non-terminal job per org.
- Worker task `maintenance.reindex_run` (maintenance queue): for each published document without a `ready` mirror in the target space, re-embed its `chunks.json` artifact, write new-space revision (deterministic ids, `ready`, records unpublished) via `indexing.ensure_revision/index_chunks/validate_index/mark_ready`. Progress is derived (NOT EXISTS sweep), so a crash resumes by re-running; verify compares counts and ids old vs new.
- `reindex_cutover` / `reindex_rollback` (definer, org owner/admin): one transaction swaps all `document_publications` pointers + `org_active_spaces` + job state and queues `knowledge.reconcile` per document (flags follow). Standby revisions stay `ready`; the old space is deleted only by `purge-old` (irreversible; rollback refused after).
- API (`org.update`): `POST/GET /v1/orgs/{o}/reindex-jobs[/{id}]`, `/resume`, `/cutover`, `/rollback`, `/purge-old`. Target = configured `AI_EMBEDDING_*` space.
- Deviations/follow-ups: sample queries on the unpublished space are impossible (adapter forces `published=true`), verify uses counts+ids; `reconcile_document` now also unflags `ready` revisions; retrieval/ingestion should read `org_active_spaces` (helper `service.current_space`) to pick the query-embedding space; docs published after cutover are skipped on rollback.
