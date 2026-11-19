"""Knowledge-graph identity and merge review (M05.01.03).

Contract: docs/architecture/m05-contracts.md sections 1, 3. PostgreSQL is the authority for entity
identity and review; RushDB holds the rebuildable graph projection.

* ``graph_entities``: one row per canonical identity of a workspace (deterministic id, see
  ``app.modules.graph.ids``). ``state`` ``active | merged | rejected``; an accepted merge marks the
  loser ``merged`` with ``merged_into`` (the alias mapping; the winner's ``aliases`` gains the
  loser's names). Same name with another type is a different row.
* ``entity_merge_candidates``: fuzzy matches awaiting a human decision (``entity_a < entity_b``,
  unique pair, so a rejected pair never comes back as pending).
* ENABLE + FORCE RLS on ``app.current_org_id`` (no ``TO`` clause, binds every role), composite FKs
  pin ``org_id`` to the workspace. **No DELETE grant to anyone.** The worker (extraction) may
  INSERT and update ``aliases``; the API role may only update the review columns.

Revision ID: 0008_graph
Revises: 0007_chat_and_ai_policy
Create Date: 2026-11-19
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0008_graph"
down_revision: str | None = "0007_chat_and_ai_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TABLES = ("graph_entities", "entity_merge_candidates")

ENTITY_TYPES = (
    "('person', 'organization', 'project', 'technology', 'concept', 'location', 'event', "
    "'document_ref', 'other')"
)


def _role(name: str, env_var: str, default: str) -> str:
    value = (
        context.config.attributes.get(name)
        or context.get_x_argument(as_dictionary=True).get(name)
        or os.environ.get(env_var)
        or default
    )
    if not ROLE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {name} name")
    return str(value)


def _app_role() -> str:
    return _role("app_role", "POSTGRES_APP_USER", "ukos_app")


def _worker_role() -> str:
    return _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")


SCHEMA_SQL = f"""
CREATE TABLE graph_entities (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    type text NOT NULL,
    canonical_name text NOT NULL,
    normalized_name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    state text NOT NULL DEFAULT 'active',
    merged_into uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_graph_entities PRIMARY KEY (id),
    CONSTRAINT uq_graph_entities_id_org_id UNIQUE (id, org_id),
    CONSTRAINT uq_graph_entities_id_workspace_id UNIQUE (id, workspace_id),
    CONSTRAINT ck_graph_entities_type CHECK (type IN {ENTITY_TYPES}),
    CONSTRAINT ck_graph_entities_state CHECK (state IN ('active', 'merged', 'rejected')),
    CONSTRAINT ck_graph_entities_merged_into CHECK (
        (state = 'merged') = (merged_into IS NOT NULL) AND merged_into IS DISTINCT FROM id
    ),
    CONSTRAINT ck_graph_entities_canonical_name CHECK (length(canonical_name) BETWEEN 1 AND 200),
    CONSTRAINT fk_graph_entities_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_graph_entities_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_graph_entities_merged_into FOREIGN KEY (merged_into, workspace_id)
        REFERENCES graph_entities (id, workspace_id)
);
CREATE INDEX ix_graph_entities_workspace_id_normalized_name
    ON graph_entities (workspace_id, normalized_name);

CREATE TABLE entity_merge_candidates (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    entity_a uuid NOT NULL,
    entity_b uuid NOT NULL,
    score double precision NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    decided_by uuid,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_entity_merge_candidates PRIMARY KEY (id),
    CONSTRAINT uq_entity_merge_candidates_pair UNIQUE (entity_a, entity_b),
    CONSTRAINT uq_entity_merge_candidates_id_workspace_id UNIQUE (id, workspace_id),
    CONSTRAINT ck_entity_merge_candidates_order CHECK (entity_a < entity_b),
    CONSTRAINT ck_entity_merge_candidates_score CHECK (score >= 0 AND score <= 1),
    CONSTRAINT ck_entity_merge_candidates_state CHECK (
        state IN ('pending', 'accepted', 'rejected')
    ),
    CONSTRAINT ck_entity_merge_candidates_decided CHECK (
        (state = 'pending') = (decided_at IS NULL)
    ),
    CONSTRAINT fk_entity_merge_candidates_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_entity_merge_candidates_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_entity_merge_candidates_entity_a FOREIGN KEY (entity_a, workspace_id)
        REFERENCES graph_entities (id, workspace_id),
    CONSTRAINT fk_entity_merge_candidates_entity_b FOREIGN KEY (entity_b, workspace_id)
        REFERENCES graph_entities (id, workspace_id),
    CONSTRAINT fk_entity_merge_candidates_decided_by FOREIGN KEY (decided_by) REFERENCES users (id)
);
CREATE INDEX ix_entity_merge_candidates_workspace_state
    ON entity_merge_candidates (workspace_id, state, score);
"""

RLS_SQL = f"""
CREATE POLICY graph_entities_select ON graph_entities FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY graph_entities_insert ON graph_entities FOR INSERT WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY graph_entities_update ON graph_entities FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY entity_merge_candidates_select ON entity_merge_candidates FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY entity_merge_candidates_insert ON entity_merge_candidates FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY entity_merge_candidates_update ON entity_merge_candidates FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
"""


def _app_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT ON graph_entities, entity_merge_candidates TO "{role}";
GRANT UPDATE (aliases, state, merged_into) ON graph_entities TO "{role}";
GRANT UPDATE (state, decided_by, decided_at) ON entity_merge_candidates TO "{role}";
"""


def _worker_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT, INSERT ON graph_entities, entity_merge_candidates TO "{role}";
GRANT UPDATE (aliases) ON graph_entities TO "{role}";
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(_app_grants_sql(_app_role()))
    op.execute(_worker_grants_sql(_worker_role()))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_merge_candidates")
    op.execute("DROP TABLE IF EXISTS graph_entities")
