#!/usr/bin/env bash
# Run a command inside a Linux container with the repo mounted, for tests that need compiled
# Python extensions the Windows host blocks (psycopg binary, mypy). DC-018.
#
#   scripts/dev/linux-runner/run.sh <relative-dir> "<command>"
#
# Examples (from anywhere):
#   scripts/dev/linux-runner/run.sh apps/api "python -m pytest -q tests"
#   scripts/dev/linux-runner/run.sh apps/api "python -m mypy app tests migrations"
#   UKOS_TEST_COMPOSE_PROJECT=ukos-v-x POSTGRES_HOST_PORT=35801 REDIS_HOST_PORT=36801 \
#     scripts/dev/linux-runner/run.sh apps/api "python -m pytest -q ../../tests/integration/m01_tenancy"
#
# How it works:
#  * The repo is mounted at Docker Desktop's own VM path for the same folder, so the compose
#    files that tests start through the host Docker socket resolve their bind mounts.
#  * --network host: tests reach published ports on 127.0.0.1 inside the Docker Desktop VM,
#    which forwards to the same ports on Windows.
#  * The venv and uv cache live in named volumes (fast, and outside the blocked D: drive).
#  * Env vars UKOS_*, POSTGRES_*, REDIS_*, API_*, WEB_*, OIDC_* are passed through.
set -euo pipefail

REL="${1:?usage: run.sh <relative-dir> \"<command>\"}"
shift
CMD="$*"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then
  ROOT_WIN="$(cygpath -m "$ROOT")"                       # D:/Career/...
  DRIVE="$(printf '%s' "$ROOT_WIN" | cut -c1 | tr 'A-Z' 'a-z')"
  ROOT_VM="/run/desktop/mnt/host/${DRIVE}${ROOT_WIN#?:}" # /run/desktop/mnt/host/d/Career/...
else
  ROOT_WIN="$ROOT"; ROOT_VM="$ROOT"                      # Linux/macOS: same path
fi
IMAGE="ukos-linux-runner:local"
# One venv volume PER checkout (hash of its path): parallel worktrees can have different lock files, and
# a shared venv would make concurrent `uv sync` runs uninstall each other's packages.
# Remove a checkout's volume with: docker volume rm ukos-runner-venv-<hash>   (list: docker volume ls | grep runner)
VENV_VOL="ukos-runner-venv-$(printf '%s' "$ROOT_WIN" | md5sum | cut -c1-10)"
export MSYS_NO_PATHCONV=1

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE (first run only)..." >&2
  HERE_NATIVE="$HERE"; command -v cygpath >/dev/null 2>&1 && HERE_NATIVE="$(cygpath -m "$HERE")"
  docker build -q -t "$IMAGE" "$HERE_NATIVE" >/dev/null
fi

ENV_ARGS=()
while IFS='=' read -r name _; do
  case "$name" in UKOS_*|POSTGRES_*|REDIS_*|API_*|WEB_*|OIDC_*) ENV_ARGS+=(-e "$name");; esac
done < <(env)

exec docker run --rm --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$ROOT_WIN:$ROOT_VM" \
  -v "$VENV_VOL:/venv" \
  -v ukos-runner-uvcache:/root/.cache/uv \
  -e UV_PROJECT_ENVIRONMENT=/venv -e UV_LINK_MODE=copy \
  "${ENV_ARGS[@]}" \
  -w "$ROOT_VM/$REL" \
  "$IMAGE" sh -c "cd \"$ROOT_VM/apps/api\" && uv sync -q --frozen && cd \"$ROOT_VM/$REL\" && . /venv/bin/activate && $CMD"
