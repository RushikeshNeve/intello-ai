#!/usr/bin/env bash
# Quiet full verification on main: unit suite + strict mypy (+ optional DB suites). Prints one line each.
#   bash scripts/dev/verify.sh            # unit + mypy
#   bash scripts/dev/verify.sh db NAME    # also run tests/integration/NAME (own project/ports)
set -u
export MSYS_NO_PATHCONV=1
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
R=scripts/dev/linux-runner/run.sh
bash $R apps/api "python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -1; python -m mypy app tests migrations 2>&1 | tail -1"
if [ "${1:-}" = "db" ]; then
  UKOS_TEST_COMPOSE_PROJECT=ukos-verify POSTGRES_HOST_PORT=35980 REDIS_HOST_PORT=36980 \
    bash $R apps/api "python -m pytest -q -p no:cacheprovider ../../tests/integration/${2:?suite} 2>&1 | tail -1"
fi
