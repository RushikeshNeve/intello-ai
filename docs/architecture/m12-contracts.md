# M12 contracts

## M12.03

Workspace golden sets (feature E1). Migration `0017_evals` (down_revision `0011_member_preapprovals`; the coordinator re-chains).
- Tables `eval_sets`, `eval_cases`, `eval_runs`, `eval_run_results`: FORCE RLS; visible/writable only to a workspace admin (`memory_ws_access(workspace_id) = 'admin'`, incl. org owner/admin) of the row's workspace in the context org.
- API `/v1/orgs/{o}/workspaces/{w}/evals/sets[/{id}[/cases[/{c}]|/runs[/{r}]]]`; all `workspace.update` (admin): viewer/editor 403, non-member/outsider/other workspace or org 404. Max 50 cases per set (409 `set_full`); each case change or rename bumps `version`.
- `POST .../runs {top_k=5, min_score=0, mode=hybrid}` is SYNCHRONOUS (bounded by 50 cases) and retrieval-only: `retrieval.retrieve` with a scope narrowed to this workspace (`resolve_scope(requested=[w])`), run as the calling admin, evidence from other workspaces is also dropped. No LLM; `config.model/policy/prompt_version` are null and reserved.
- Per case: `answer_status` answered | abstained (no evidence >= `min_score`) | error; metrics `hit`, `rank`, `abstain_correct`, `passed`. Summary: `hit_at_k`, `mrr`, `abstention_accuracy`, `abstain_correct_rate`, `pass_rate`.
- Web: `/o/{org}/w/{ws}/evals` (nav "Golden sets" under Manage).
