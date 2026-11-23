#!/usr/bin/env bash
# M14.08 key rotation drill around `python -m app.core.crypto.rotate` (app/core/crypto/rotate.py).
#   bash scripts/ops/rotate-keys.sh kek [--dry-run]              re-wrap every org data key under the active master key
#   bash scripts/ops/rotate-keys.sh dek --org <uuid> [--dry-run]  new data key for one org + batched re-encryption
#   bash scripts/ops/rotate-keys.sh reencrypt [--org <uuid>]      re-seal legacy single-key rows / retired-key rows
#   bash scripts/ops/rotate-keys.sh verify [--org <uuid>]         every key unwraps, every value opens
#   bash scripts/ops/rotate-keys.sh shred --org <uuid> --yes      crypto-shred: delete the org's data keys (irreversible)
# Every changing command runs a dry run first and a verification after; a failed verification exits non-zero.
# KEK rotation: put the new key in MASTER_KEY_FILE + new MASTER_KEY_ID, the old one in MASTER_OLD_KEYS=id:base64,
# run `kek`, then drop MASTER_OLD_KEYS. Re-encryption is online (batches with SKIP LOCKED) and resumable: rerun it.
# Runs the module from apps/api with the CURRENT environment (DATABASE_MIGRATION_URL, MASTER_*, CONNECTOR_*),
# or inside the compose project with ROTATE_IN_COMPOSE=1 OPS_PROJECT=<name> (api-migrate image) [U: untested].
# Output is counts and ids only; key material is never printed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MSYS_NO_PATHCONV=1

rot() {
  if [[ "${ROTATE_IN_COMPOSE:-0}" == "1" ]]; then
    : "${OPS_PROJECT:?set OPS_PROJECT}"
    docker compose -p "$OPS_PROJECT" --project-directory "$ROOT" run --rm --no-deps \
      -e MASTER_KEY_FILE -e MASTER_KEY_ID -e MASTER_OLD_KEYS -e KEY_PROVIDER \
      -e CONNECTOR_ENCRYPTION_KEY -e CONNECTOR_ENCRYPTION_KEY_ID -e CONNECTOR_ENCRYPTION_OLD_KEYS \
      api-migrate python -m app.core.crypto.rotate "$@"
  else
    (cd "$ROOT/apps/api" && python -m app.core.crypto.rotate "$@")
  fi
}

CMD="${1:?usage: rotate-keys.sh kek|dek|reencrypt|verify|shred [--org UUID] [--dry-run] [--yes]}"; shift
ORG=(); DRY=0; YES=()
while [[ $# -gt 0 ]]; do case "$1" in
  --org) ORG=(--org "$2"); shift 2;; --dry-run) DRY=1; shift;; --yes) YES=(--yes); shift;;
  *) echo "unknown argument $1" >&2; exit 2;; esac; done

case "$CMD" in
  kek)
    rot kek --dry-run
    [[ "$DRY" == "1" ]] && exit 0
    rot kek
    rot verify ;;
  dek)
    [[ ${#ORG[@]} -eq 2 ]] || { echo "dek needs --org" >&2; exit 2; }
    rot reencrypt "${ORG[@]}" --dry-run
    [[ "$DRY" == "1" ]] && exit 0
    rot new-dek "${ORG[@]}"
    rot reencrypt "${ORG[@]}"
    rot verify "${ORG[@]}" ;;
  reencrypt)
    rot reencrypt "${ORG[@]}" --dry-run
    [[ "$DRY" == "1" ]] && exit 0
    rot reencrypt "${ORG[@]}"
    rot verify "${ORG[@]}" ;;
  verify) rot verify "${ORG[@]}" ;;
  shred)
    [[ ${#ORG[@]} -eq 2 ]] || { echo "shred needs --org" >&2; exit 2; }
    rot shred "${ORG[@]}" "${YES[@]}" ;;
  *) echo "unknown command $CMD" >&2; exit 2 ;;
esac
