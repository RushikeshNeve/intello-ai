# Prompt changelog

Every change to `apps/api/app/modules/prompts/defaults/*.txt` or the fixed scaffold
(`prompts/scaffold.py`) needs a new entry here that names the changed kind
(`chat_system`, `graph_extract`, `research_plan`; a scaffold change names all affected kinds), plus a
passing `tests/evaluation/run_eval.py --require-baseline --metrics`. Enforced by `scripts/ci/prompt_gate.sh`.

- 2026-11-21 chat_system, graph_extract, research_plan: versioned registry introduced (M12.01); no wording change.
