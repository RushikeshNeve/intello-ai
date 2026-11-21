# Model and embedding comparison (M12.08)

Compare two configurations on the same data before switching models or running the reindex tool. A is the baseline, B the candidate. Reports land in `reports/compare/<mode>.md|json`.

```
bash scripts/eval/compare.sh chat|embedding|rerank --a <cfg> --b <cfg> [--fail-on-regression] [--prices-json '{...}']
```

A config is `stub:<x>`, `noop` (reranker) or a JSON object: `base_url`, `model`, `provider` (`local` default, or `openai` for hosted), `api_key_env` (name of the env var holding the key, never the key itself), and for embeddings `dimensions`, `query_prefix`, `document_prefix`.

## Chat: LM Studio gemma vs OpenAI

```
export OPENAI_API_KEY=...   # only for the hosted side
bash scripts/eval/compare.sh chat \
  --a '{"base_url":"http://localhost:1234/v1","model":"gemma-3-4b-it"}' \
  --b '{"provider":"openai","base_url":"https://api.openai.com/v1","model":"gpt-4o-mini","api_key_env":"OPENAI_API_KEY"}' \
  --prices-json '{"openai/gpt-4o-mini":{"in":150000,"out":600000}}' --fail-on-regression
```

Answers go through the production `build_prompt` and `verify` path; read grounded_rate and faithfulness first, then p95 latency and cost. Prices are micro-USD per million tokens (same shape as `USAGE_PRICES_JSON`); local models cost 0.

## Embeddings: embedding-gemma 768d vs a 1536d hosted model

```
bash scripts/eval/compare.sh embedding \
  --a '{"base_url":"http://localhost:1234/v1","model":"text-embedding-embeddinggemma-300m","dimensions":768,"query_prefix":"task: search result | query: ","document_prefix":"title: none | text: "}' \
  --b '{"provider":"openai","base_url":"https://api.openai.com/v1","model":"text-embedding-3-small","dimensions":1536,"api_key_env":"OPENAI_API_KEY"}'
```

Each embedder builds its own temporary in-memory index, so differing dimensions are fine. Compare hit@5, MRR and context precision/recall. Only when B is not a regression, run `scripts/ops/reindex.sh` (M12.05) with `AI_EMBEDDING_*` pointing at B; that tool is the irreversible-after-purge step, this harness changes nothing in the database.

## Reading the result

Exit code 0 = no regression, 2 = regression with `--fail-on-regression` (quality drop > 0.02, p95 > 2x, cost > 2x, more failures; tune with `--max-quality-drop`, `--max-latency-ratio`, `--max-cost-ratio`), 1 = bad config. Rerankers: implement `Reranker.rerank(query, [(id, text)]) -> ids` in `run_compare.py`; today only `noop` exists. The dataset is the small `retrieval_golden` set; use `--dataset DIR` (corpus.json + queries.json) for your own documents.
