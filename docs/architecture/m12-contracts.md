# M12 contracts
## M12.01 Versioned prompt registry
- Migration `0016_prompt_versions` (down_revision `0011`; re-chain when 0012-0015 land). Table `prompt_versions` (FORCE RLS; `org_id` NULL = global default, readable by any org context, never with none). Kinds: `chat_system` (chat + research synthesis), `graph_extract`, `research_plan`. States `draft|active|retired`; one active per (org, kind); rows immutable except `state`/`activated_at`.
- Defaults are files `app/modules/prompts/defaults/<kind>.txt`; `prompt_seed_default` (definer) makes the global active row equal the file (new global version when the hash changes). `prompts.resolve(kind, org_id, session)` -> `(body, version_id, sha256)`; any storage error falls back to the file (`version_id` None).
- Only the editable instruction body is stored. Safety scaffolding is composed in code AFTER the body (`prompts.chat_system|graph_system|plan_system`); bodies with `<<<`/`>>>` fence markers or >6000 chars are rejected (422).
- Run metadata (additive, no new columns): chat `chat_runs.model` gains `prompt_version_id`, `prompt_sha256`; research `usage.prompts[kind] = {id, sha256}`; graph `extractor_version` becomes `graph-extract/1+p<sha12>` when a non-default prompt was used.
- Admin API (owner/admin, `org.update`): `GET /v1/orgs/{o}/prompts`, `POST .../{kind}/versions` (draft), `POST .../versions/{id}/activate`, `POST .../{kind}/rollback` (previous ever-active version, else back to global default). Audit: `prompt.version.created|activated|rolled_back` (ids, kind, version numbers).
- Merge note (M11.02): `ai/prompts.py` is untouched; the default `chat_system.txt` holds rules 1-5 and `scaffold.CHAT_SAFETY` holds rule 6. If M11.02 reorders `SYSTEM_PROMPT`, mirror the order in those two places.

## M12.03

Workspace golden sets (feature E1). Migration `0017_evals` (down_revision `0011_member_preapprovals`; the coordinator re-chains).
- Tables `eval_sets`, `eval_cases`, `eval_runs`, `eval_run_results`: FORCE RLS; visible/writable only to a workspace admin (`memory_ws_access(workspace_id) = 'admin'`, incl. org owner/admin) of the row's workspace in the context org.
- API `/v1/orgs/{o}/workspaces/{w}/evals/sets[/{id}[/cases[/{c}]|/runs[/{r}]]]`; all `workspace.update` (admin): viewer/editor 403, non-member/outsider/other workspace or org 404. Max 50 cases per set (409 `set_full`); each case change or rename bumps `version`.
- `POST .../runs {top_k=5, min_score=0, mode=hybrid}` is SYNCHRONOUS (bounded by 50 cases) and retrieval-only: `retrieval.retrieve` with a scope narrowed to this workspace (`resolve_scope(requested=[w])`), run as the calling admin, evidence from other workspaces is also dropped. No LLM; `config.model/policy/prompt_version` are null and reserved.
- Per case: `answer_status` answered | abstained (no evidence >= `min_score`) | error; metrics `hit`, `rank`, `abstain_correct`, `passed`. Summary: `hit_at_k`, `mrr`, `abstention_accuracy`, `abstain_correct_rate`, `pass_rate`.
- Web: `/o/{org}/w/{ws}/evals` (nav "Golden sets" under Manage).

## M12.05
Re-embedding / index migration (migration `0018_reindex`, `modules/knowledge/reindex/`, `scripts/ops/reindex.sh`).
- PG is the authority: `reindex_jobs` (state planned|running|verifying|ready|cutover|rolled_back|failed, counts, lease + `lease_epoch` fencing) and `org_active_spaces` (active-space pointer; absent = configured default). One non-terminal job per org.
- Worker task `maintenance.reindex_run` (maintenance queue): for each published document without a `ready` mirror in the target space, re-embed its `chunks.json` artifact, write new-space revision (deterministic ids, `ready`, records unpublished) via `indexing.ensure_revision/index_chunks/validate_index/mark_ready`. Progress is derived (NOT EXISTS sweep), so a crash resumes by re-running; verify compares counts and ids old vs new.
- `reindex_cutover` / `reindex_rollback` (definer, org owner/admin): one transaction swaps all `document_publications` pointers + `org_active_spaces` + job state and queues `knowledge.reconcile` per document (flags follow). Standby revisions stay `ready`; the old space is deleted only by `purge-old` (irreversible; rollback refused after).
- API (`org.update`): `POST/GET /v1/orgs/{o}/reindex-jobs[/{id}]`, `/resume`, `/cutover`, `/rollback`, `/purge-old`. Target = configured `AI_EMBEDDING_*` space.
- Deviations/follow-ups: sample queries on the unpublished space are impossible (adapter forces `published=true`), verify uses counts+ids; `reconcile_document` now also unflags `ready` revisions; retrieval/ingestion should read `org_active_spaces` (helper `service.current_space`) to pick the query-embedding space; docs published after cutover are skipped on rollback.

## M12.02/04
- RAG metrics live in `tests/evaluation/metrics/rag_metrics.py` (pure functions; no `__init__.py` because `tests/evaluation/metrics.py` owns the name `metrics`; import `rag_metrics` with that dir on `sys.path`). Metrics: context precision/recall (document level), faithfulness and citation validity (lexical support, 0.6 of claim content words; optional `Judge.supports(claim, evidence)` hook), answer relevance (question-word coverage proxy), abstention correctness.
- `run_eval.py --metrics` records calls through `report.Recorder` and writes `reports/metrics.json|md`; gate semantics and `baseline.json` untouched (new metrics are report-only, no baseline committed).
- `tests/evaluation/budgets.py` measures p50/p95 per-question latency (retrieval+answer) and tokens per answer (deterministic word-token proxy: the stub has no accounting) against `budgets.json` with tolerances (latency limit x4, tokens +10%). `--update` rewrites it explicitly. Wired into `scripts/ci/run_local.sh` (gate step) and the ci.yml job as an extra step.
- Real-model runs later: swap the proxy for provider usage counts and pass a judge to the metrics.

## M12.08
- Comparison harness: `tests/evaluation/compare/run_compare.py` (+ `scripts/eval/compare.sh`), modes `chat | embedding | rerank`, A (baseline) vs B (candidate); output `reports/compare/<mode>.{json,md}`.
- Config = `stub:<x>` (stubs in `compare/stubs.py`), `noop` (rerank baseline) or JSON (`base_url`, `model`, `provider` local|openai, `api_key_env`, `dimensions`, prefixes).
- Chat uses the real path (`build_prompt` -> adapter -> `verify`): grounded_rate, faithfulness, answer_relevance. Embedding builds a temporary in-memory index per embedder: hit@5, MRR, context precision/recall (`rag_metrics`). `Reranker.rerank(query, [(id, text)]) -> ids`; only `NoopReranker` and a stub exist.
- Columns: p50/p95 latency, tokens in/out (adapter `last_usage` else ceil(chars/4)), cost from a `USAGE_PRICES_JSON`-shaped table (`--prices-json`), failures. Dataset = `retrieval_golden` (`--dataset DIR` overrides).
- `--fail-on-regression` exits 2 when B drops a metric > 0.02, p95 > x2, cost > x2 or has more failures than A (all tunable). Host pytest needs `-p no:hypothesispytest`.
