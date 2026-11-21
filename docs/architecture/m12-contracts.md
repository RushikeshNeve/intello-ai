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
