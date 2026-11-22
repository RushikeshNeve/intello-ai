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

## M12.08 Comparison harness
- Comparison harness: `tests/evaluation/compare/run_compare.py` (+ `scripts/eval/compare.sh`), modes `chat | embedding | rerank`, A (baseline) vs B (candidate); output `reports/compare/<mode>.{json,md}`.
- Config = `stub:<x>` (stubs in `compare/stubs.py`), `noop` (rerank baseline) or JSON (`base_url`, `model`, `provider` local|openai, `api_key_env`, `dimensions`, prefixes).
- Chat uses the real path (`build_prompt` -> adapter -> `verify`): grounded_rate, faithfulness, answer_relevance. Embedding builds a temporary in-memory index per embedder: hit@5, MRR, context precision/recall (`rag_metrics`). `Reranker.rerank(query, [(id, text)]) -> ids`; only `NoopReranker` and a stub exist.
- Columns: p50/p95 latency, tokens in/out (adapter `last_usage` else ceil(chars/4)), cost from a `USAGE_PRICES_JSON`-shaped table (`--prices-json`), failures. Dataset = `retrieval_golden` (`--dataset DIR` overrides).
- `--fail-on-regression` exits 2 when B drops a metric > 0.02, p95 > x2, cost > x2 or has more failures than A (all tunable). Host pytest needs `-p no:hypothesispytest`.

## M12.06 Prompt change gate
- Migration `0019a_prompt_gate` (after `0015b_cache_evict_orgs`): `prompt_versions.eval_run_id|eval_attestation` (nullable evidence) and `prompt_gate_settings(org_id, enforced=false, min_pass_rate=0.8)` with RLS.
- `modules/prompts/activate.py::check_gate`: when `enforced`, `POST .../versions/{id}/activate` needs body `{eval_run_id}` (own-org completed run with `summary.pass_rate >= min_pass_rate`) or `{attestation_id}` (`<sha256>.<hmac>` over `kind|sha256`, key env `PROMPT_ATTESTATION_SECRET`; unset = attestations rejected). Otherwise 409 `eval_required` (no detail). Evidence is stored on the row.
- Rollback and the global default never go through the gate. Activating an already-active version is a no-op.
- `GET|PUT /v1/orgs/{o}/prompts/gate` (`org.update`): `{enforced, min_pass_rate}`; default off.
- CI: `scripts/ci/prompt_gate.sh|py` (PR only): changes under `prompts/defaults/` or `scaffold.py` need a new `docs/release/PROMPT_CHANGELOG.md` line naming each kind and a passing `run_eval.py --require-baseline --metrics`.

## M12.07 Workspace answer styles
- Migration `0019b_workspace_styles` (down `0019a_prompt_gate`). Table `workspace_styles` (FORCE RLS; `workspace_id` NULL = org default; unique per workspace and per org default): tone neutral|formal|friendly|technical, answer_format concise|detailed|bullets|table, language (letters only, 2-40), citation_style numeric|inline, custom_instructions <=1000. Read: workspace readers (org default: org context); write: workspace admin; org default written by the API after `org.update`. Worker: SELECT.
- `prompts/styles.py`: `compose(system, style)` appends a fenced `<<<STYLE PREFERENCES>>>` block AFTER the scaffold (body + CHAT_SAFETY) stating it never overrides rules 1-6. Input with `<<<`/`>>>`/control chars/oversize/odd language is rejected (422 `invalid_style`); stored rows are re-stripped at composition. No wording heuristics: scaffold and verifier stay authoritative.
- Resolution (chat + research synthesis): run scoped to exactly one workspace -> its row, else org default, else none; storage error -> unstyled prompt. Hook is one `styles.compose(...)` call in `ai/chat.py` and `research/runtime.py`.
- API: `GET/PUT /v1/orgs/{o}/workspaces/{w}/style` (ws admin writes, members read), `GET/PUT /v1/orgs/{o}/style`. Audit `style.updated` (scope + ids only). Web: "Answer style" card in workspace settings.

## M12.09 Prompt A/B shadow runs
Prompt A/B shadow runs (migration `0019c_shadow_runs`, down_revision `0019b_workspace_styles`). Flag `PROMPT_SHADOW_ENABLED` (default false); hard per-org daily cap `PROMPT_SHADOW_MAX_PER_DAY` (200; no usage-quota module exists to honour).
- Tables (FORCE RLS by org): `shadow_candidates` (one per org+kind, kind `chat_system` only, `rate_pct` 0-20, 0 = off; version must be an own DRAFT) and `shadow_runs` (ids, statuses, numbers; NEVER question/answer/evidence text). Admin-only reads are enforced in the API (`org.update`); RLS is org-level because the worker writes as the run creator.
- Flow: `chat_tasks.run_chat`, after a model-answered run is `completed`, hands an in-memory `ShadowSeed` (same evidence, question, live numbers, `still_valid` re-check) to `evals.shadow_runner.maybe_shadow`. Sampled -> row `running`, then a daemon thread re-generates with the candidate body on THAT evidence (no retrieval), verifies, stores status / citation validity / latency / tokens / outcome win|tie|loss (status rank grounded>partial>abstained, then citation validity). Evidence deleted or access lost before or after the candidate call -> `skipped evidence_deleted`; any error -> `failed error`. Evidence is not persisted, so shadowing runs in-process in the chat worker (not a separate Celery task); no usage events for shadow calls (tokens are on the row).
- API `/v1/orgs/{o}/prompt-shadow`: `GET`, `PUT|DELETE /{kind}`, `GET /runs?candidate_id=`, `GET /results` (per candidate counts, wins/ties/losses, mean `latency_delta_ms` = candidate - live). Other org's version 404; non-draft 409 `not_a_draft`; rate > 20 422.

## Follow-ups
- Active space: `RetrievalScope.active_space` is read from `org_active_spaces` when the scope is minted (search, chat, research all share it); no row = configured `AI_EMBEDDING_*` space. The vector leg queries that space and the result-cache key uses it, so cutover/rollback change what is queried.
- The query embedder must produce the active space (`embedder.spec.space_id == active_space`); otherwise vector/hybrid raise `EmbeddingSpaceMismatch` (a `SearchUnavailable`: 503 `search_unavailable`, nothing embedded or queried). Keyword mode is unaffected. A different model per space needs the old model configured again; spaces are hashes, so the model cannot be derived from the id.
- Ingestion: new jobs (`documents/finalize.py`, `remote_ingest.py`) take `current_space(session, org, configured)`. If the worker's embedder does not produce it the job fails permanently `embedding_space_mismatch` (retry after fixing config).
- During a running reindex, new/changed documents are embedded into the ACTIVE (old) space only; the job's derived-progress sweep (MAX_ROUNDS + verify) mirrors them into the target space before cutover (simplest correct; no dual-write).
- Eviction: dispatcher queues `maintenance.embedding_cache_evict` every `EMBEDDING_CACHE_EVICT_INTERVAL_SECONDS` (default 3600) only when `EMBEDDING_CACHE_ENABLED`; the task runs `evict_all` (per-org `evict_org` with `EMBEDDING_CACHE_MAX_ROWS_PER_ORG` / `_MAX_AGE_DAYS`). Migration `0015b_cache_evict_orgs` (chained after 0018) adds definer `embedding_cache_orgs(limit)` so the worker can enumerate orgs.

## Wave B integration notes
- Migration chain (linear, single head): `0018_reindex -> 0015_usage_limits -> 0015b_cache_evict_orgs -> 0019a_prompt_gate -> 0019b_workspace_styles -> 0019c_shadow_runs`.
- Shadow runs skip (no row) when the org is at a blocking `chat` quota (`shadow_runner._over_quota`, fails open on storage errors). The shadow model call itself is not metered (estimates stay on the row).
- `ShadowSeed.style` carries the live run's answer style, so the candidate prompt is `styles.compose(chat_system(candidate), style)`, the same path as the live prompt. Test: `m04_chat/test_shadow.py::test_style_registry_and_shadow_compose_without_interfering`.
- `prompt_gate_settings` has an ORM model (`PromptGateSettings`) and sits in `ORG_SCOPED_TABLES`; Wave B tenancy coverage is `tests/security/postgres_rls/test_20_wave_b_tables.py`.
