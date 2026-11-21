
## M12.02/04
- RAG metrics live in `tests/evaluation/metrics/rag_metrics.py` (pure functions; no `__init__.py` because `tests/evaluation/metrics.py` owns the name `metrics`; import `rag_metrics` with that dir on `sys.path`). Metrics: context precision/recall (document level), faithfulness and citation validity (lexical support, 0.6 of claim content words; optional `Judge.supports(claim, evidence)` hook), answer relevance (question-word coverage proxy), abstention correctness.
- `run_eval.py --metrics` records calls through `report.Recorder` and writes `reports/metrics.json|md`; gate semantics and `baseline.json` untouched (new metrics are report-only, no baseline committed).
- `tests/evaluation/budgets.py` measures p50/p95 per-question latency (retrieval+answer) and tokens per answer (deterministic word-token proxy: the stub has no accounting) against `budgets.json` with tolerances (latency limit x4, tokens +10%). `--update` rewrites it explicitly. Wired into `scripts/ci/run_local.sh` (gate step) and the ci.yml job as an extra step.
- Real-model runs later: swap the proxy for provider usage counts and pass a judge to the metrics.
