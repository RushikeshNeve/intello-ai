# RAG evaluation harness (M04.03.04 and M04.03.05)

Code: `tests/evaluation/`. Spec: `docs/architecture/m04-contracts.md` section 10. Offline, no model, no network, no Docker stack.

## Run

From the repo root, on this Windows host through the Linux runner (DC-018):

```bash
export MSYS_NO_PATHCONV=1
bash scripts/dev/linux-runner/run.sh apps/api "python ../../tests/evaluation/run_eval.py --require-baseline"
bash scripts/dev/linux-runner/run.sh apps/api "python -m pytest -q ../../tests/evaluation"
bash scripts/dev/linux-runner/run.sh apps/api "MYPYPATH=../../tests/evaluation:. python -m mypy ../../tests/evaluation --explicit-package-bases"
```

`run_eval.py` writes `tests/evaluation/reports/eval.json` (schema version 1, per-item results and aggregates, gate verdict)
and `eval.md`; both are git-ignored. Exit code 0 = gate passed, 1 = failed, 2 = usage/data error.

## What it does

1. Builds the corpus (`corpus/manifest.json` + `corpus/files/`) through the real processors and `chunk_document`, embeds with
   `FakeEmbeddingClient`, indexes into `InMemoryKnowledgeRepository` (latest version published, older versions unpublished,
   deleted documents purged).
2. For each dataset item (`datasets/golden.jsonl`, schema `datasets/schema.json`) it runs `Retriever.retrieve(query, workspace_ids)`
   under the persona's scope, then `Answerer.answer(question, retrieved)`.
3. Computes recall@5/10, MRR, nDCG@10, hit rate, permission leakage, stale exposure (deleted or superseded chunks retrieved or cited),
   citation validity, abstention precision/recall, answer accuracy, injection resistance, updated-fact correctness, latency percentiles.

## Gates

Hard (absolute, never relaxed): `permission_leakage == 0`, `stale_exposure == 0`, `citation_validity == 1.0`,
`injection_resistance == 1.0`. Regression: every other quality metric must stay within a per-metric tolerance (default 0.02) of
`baseline.json`. A baseline is keyed by the adapter pair, and records the dataset and corpus hashes: changing either makes the baseline
stale (the gate fails until `--update-baseline` is run and the diff reviewed). `--require-baseline` (CI) fails when there is no entry.

Citation validity: every cited chunk exists, is published, is inside the persona's scope, belongs to the cited document, was among the
retrieved chunks, and the quoted excerpt occurs in the chunk. An answer without any citation counts as invalid.

## Switching to the real M04 modules (after merge)

```bash
python ../../tests/evaluation/run_eval.py \
  --retriever app.modules.retrieval.<module>:<factory> --answerer app.modules.ai.<module>:<factory> --update-baseline
```

A factory takes the built `EvalEnv` (`repo`, `embedder`, `space_id`, `scopes[org]`, `workspace_uuid[ws]`, `chunks`) and returns an object
with `retrieve(query, workspace_ids: Sequence[str]) -> list[RetrievedChunk]` (the real adapter maps manifest workspace ids to UUIDs,
builds a `RetrievalScope` and calls `retrieve_candidates` / `fuse` / `deduplicate`, then maps hits back with `env.chunks`) and
`answer(question, retrieved) -> Answer(text, citations, status)` (status `answered|partial|abstained`; the real adapter calls
`build_prompt`, a scripted `FakeChatModel` and `verify`). Types live in `tests/evaluation/adapters.py`. The new run gets its own entry in
`baseline.json`; the baseline adapter entry stays as a floor reference.

## Your own corpus

Copy the layout `corpus/manifest.json` + `corpus/files/*` (supported types: pdf, txt, md, html), write a dataset `.jsonl` using
`datasets/schema.json`, and run with `--corpus DIR --dataset FILE --baseline OTHER.json`. The format of the ChatGPT sample-corpus prompt
(`manifest.csv`, `questions.jsonl`) has no workspaces or personas, so it needs a one-off conversion script (assign documents to workspaces,
add personas, map `type` to `kind`, set `expect_abstain` for "not answerable"); not written yet.

## Live mode

`--live` is a stub. It refuses unless `EVAL_ALLOW_LIVE=1`, and even then exits 2 (not implemented). Intended: local LM Studio model for
answers and judge; an OpenAI judge only when `OPENAI_API_KEY` is set and `AI_EXTERNAL_PROVIDERS_ENABLED=true`. Never required.

## Known limits

- The fake embedder is a hashed bag of words: the vector leg adds little beyond BM25, so recall here says little about the real model.
- The baseline answerer quotes sentences; its answer accuracy (about 0.78) and abstention precision (0.80) are a floor, not a target.
- Persona scope is given by the manifest, not derived from PostgreSQL; revalidation is modelled by the chunk-state registry.
