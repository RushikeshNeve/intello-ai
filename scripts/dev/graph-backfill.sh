#!/usr/bin/env bash
# Build the knowledge graph for documents that were published BEFORE GRAPH_ENABLED was turned on.
# Queues one `graph.extract_revision` task per currently-published document of a workspace (idempotent).
#   bash scripts/dev/graph-backfill.sh <org-slug> <workspace-slug>      (dev stack, compose project `ukos`)
set -euo pipefail
export MSYS_NO_PATHCONV=1
ORG="${1:?org slug}"; WS="${2:?workspace slug}"
ROWS=$(docker exec ukos-postgres-1 psql -U ukos_admin -d ukos -At -F, -c "select w.org_id, p.document_id, p.revision_id from document_publications p join documents d on d.id=p.document_id and d.deleted_at is null join workspaces w on w.id=d.workspace_id join organizations o on o.id=w.org_id where o.slug='${ORG//\'/}' and w.slug='${WS//\'/}'")
[ -n "$ROWS" ] || { echo "no published documents found for $ORG/$WS"; exit 1; }
echo "$ROWS" | docker compose exec -T worker python -c "
import sys
from app.workers.celery_app import celery_app
n = 0
for line in sys.stdin:
    org, doc, rev = line.strip().split(',')
    celery_app.send_task('graph.extract_revision', args=[org, doc, rev], queue='maintenance')
    n += 1
print('queued', n, 'graph extraction task(s)')
"
