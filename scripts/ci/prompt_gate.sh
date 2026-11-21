#!/usr/bin/env bash
# Prompt change gate (M12.06): BASE=origin/main bash scripts/ci/prompt_gate.sh
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec python scripts/ci/prompt_gate.py --base "${BASE:-origin/main}" "$@"
