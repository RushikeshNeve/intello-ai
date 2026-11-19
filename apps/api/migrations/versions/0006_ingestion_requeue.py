"""Manual retry of an ingestion job: the ``ingest_requeue_job`` definer function (M03.03.04).

The API role may only SELECT/INSERT ingestion jobs (a user must never be able to rewrite job
state), so "retry" is one narrow, audited-by-the-caller state transition done in a SECURITY
DEFINER function instead of a broader UPDATE grant:

* owned by the migration user, ``search_path = pg_catalog, public, pg_temp`` (pinned), every
  relation schema-qualified, ``REVOKE ALL ... FROM PUBLIC``, ``EXECUTE`` to the **app role only**;
* acts only on the caller's own org (``app.current_org_id``, set by the tenant transaction) and
  only on a job that is ``dead``, or ``unsupported`` for a reason other than ``no_text_extracted``
  (an empty document stays terminal), whose document is not deleted; anything else returns 0 rows
  (the API answers 409). The row lock makes concurrent retries single-winner;
* resets ``attempts``/``run_after``/error columns/lease columns and **never decreases**
  ``lease_epoch`` (the fencing token stays monotonic, so a stale worker still cannot write);
* writes a ``queued`` job event (``detail.reason = 'manual_retry'``) and an
  ``ingestion.job.queued`` outbox row in the same transaction, and puts a ``failed`` revision back
  to ``building`` so the pipeline can resume from its checkpoints.

Revision ID: 0006_ingestion_requeue
Revises: 0005_ingestion
Create Date: 2026-11-19
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0006_ingestion_requeue"
down_revision: str | None = "0005_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SIGNATURE = "ingest_requeue_job(uuid)"


def _app_role() -> str:
    value = (
        context.config.attributes.get("app_role")
        or context.get_x_argument(as_dictionary=True).get("app_role")
        or os.environ.get("POSTGRES_APP_USER")
        or "ukos_app"
    )
    if not ROLE_PATTERN.fullmatch(value):
        raise ValueError("invalid app_role name")
    return str(value)


FUNCTION_SQL = """
CREATE FUNCTION ingest_requeue_job(p_job_id uuid)
RETURNS TABLE (job_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_org uuid := nullif(current_setting('app.current_org_id', true), '')::uuid;
    v_job public.ingestion_jobs%ROWTYPE;
BEGIN
    IF v_org IS NULL OR p_job_id IS NULL THEN
        RETURN;
    END IF;
    SELECT j.* INTO v_job
    FROM public.ingestion_jobs j
    JOIN public.documents d ON d.id = j.document_id AND d.org_id = j.org_id
    WHERE j.id = p_job_id
      AND j.org_id = v_org
      AND d.deleted_at IS NULL
      AND (j.state = 'dead'
           OR (j.state = 'unsupported'
               AND COALESCE(j.last_error_code, '') <> 'no_text_extracted'))
    FOR UPDATE OF j;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    UPDATE public.ingestion_jobs j SET
        state = 'queued',
        attempts = 0,
        run_after = now(),
        lease_owner = NULL,
        lease_expires_at = NULL,
        heartbeat_at = NULL,
        current_stage = NULL,
        last_error_code = NULL,
        last_error_detail = NULL,
        finished_at = NULL,
        updated_at = now()
    WHERE j.id = v_job.id;  -- lease_epoch is deliberately untouched (monotonic fencing token)
    UPDATE public.knowledge_revisions r SET state = 'building'
    WHERE r.id = v_job.revision_id AND r.org_id = v_org AND r.state = 'failed';
    INSERT INTO public.ingestion_job_events
        (job_id, org_id, workspace_id, document_id, version_id, event_type, attempt, detail)
    VALUES (v_job.id, v_job.org_id, v_job.workspace_id, v_job.document_id, v_job.version_id,
            'queued', 0, jsonb_build_object('reason', 'manual_retry'));
    INSERT INTO public.outbox_events
        (org_id, aggregate_type, aggregate_id, event_type, payload, available_at)
    VALUES (v_job.org_id, 'ingestion_job', v_job.id, 'ingestion.job.queued',
            jsonb_build_object('job_id', v_job.id), now());
    RETURN QUERY SELECT v_job.id;
END;
$$;
"""


def upgrade() -> None:
    op.execute(FUNCTION_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO "{_app_role()}"')


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
