#!/usr/bin/env bash
# Re-embedding / index migration (M12.05). Thin wrapper over the org-admin API.
#   API_URL=https://host ORG_ID=<uuid> TOKEN=<admin bearer> bash scripts/ops/reindex.sh <cmd> [job_id]
# cmd: create | status <job> | resume <job> | cutover <job> | rollback <job> | purge-old <job>
# create plans a job into the space of the CURRENTLY CONFIGURED embedding model (AI_EMBEDDING_*).
# Flow: roll out the new model config to the workers, `create`, poll `status` until state=ready,
# `cutover`; `rollback` flips back; `purge-old` (irreversible) deletes the abandoned space.
set -euo pipefail
: "${API_URL:?set API_URL}" "${ORG_ID:?set ORG_ID}" "${TOKEN:?set TOKEN}"
cmd="${1:-}"
job="${2:-}"
base="${API_URL%/}/v1/orgs/${ORG_ID}/reindex-jobs"
call() { curl -fsS -X "$1" -H "Authorization: Bearer ${TOKEN}" "$2"; echo; }
case "$cmd" in
  create) call POST "$base" ;;
  status) : "${job:?job id required}"; call GET "$base/$job" ;;
  resume|cutover|rollback|purge-old) : "${job:?job id required}"; call POST "$base/$job/$cmd" ;;
  *) sed -n 2,8p "$0"; exit 2 ;;
esac
