#!/usr/bin/env bash
# Model / embedding / reranker comparison (M12.08). See docs/ops/model-comparison.md.
#   bash scripts/eval/compare.sh chat --a '{...}' --b '{...}' [--fail-on-regression]
#   bash scripts/eval/compare.sh embedding --a stub:0 --b stub:0.5
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec python tests/evaluation/compare/run_compare.py "$@"
