# tests/evaluation

Golden RAG evaluation set, offline harness and CI gate. Full description: `docs/development/evaluation.md`.

- `corpus/` seed corpus (26 original documents, 2 orgs, 4 workspaces) and `manifest.json`
- `datasets/golden.jsonl`, `datasets/schema.json`
- `adapters.py` Retriever/Answerer protocols and baseline adapters; `corpus_build.py` real extraction+chunking into the in-memory index
- `metrics.py`, `evaluate.py`, `gate.py`, `run_eval.py`, `baseline.json`
- `make_pdfs.py` / `pdfwriter.py` regenerate the three corpus PDFs; `reports/` is git-ignored output
- `test_eval_*.py` unit tests (run from `apps/api` through the Linux runner)
