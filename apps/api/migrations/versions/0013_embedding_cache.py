"""Embedding cache (M11.03): chunk text hash -> vector, per org.

* ``embedding_cache``: key ``(org_id, embedding_space_id, text_sha256)``; value is the vector
  (float32 little-endian bytea) plus ``created_at`` / ``last_used_at``. NEVER the text itself.
* The cache is keyed per ORG and FORCE RLS by org on purpose. A global cache would let identical
  text uploaded by two tenants share a row, so a hit/miss (timing, or row existence) would reveal
  that another tenant holds that text. Per-org keys make a hit prove only the caller's own prior
  use. The cost is no cross-tenant dedup of identical boilerplate; accepted.
* Roles: app and worker may SELECT / INSERT / DELETE and UPDATE (last_used_at); policies bind every
  command to the context org (``app.current_org_id``). Eviction is per org (size / age).

Revision ID: 0013_embedding_cache
Revises: 0012_usage_events
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0013_embedding_cache"
down_revision: str | None = "0012_usage_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


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


SCHEMA_SQL = f"""
CREATE TABLE embedding_cache (
    org_id uuid NOT NULL,
    embedding_space_id text NOT NULL,
    text_sha256 text NOT NULL,
    vector bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_embedding_cache PRIMARY KEY (org_id, embedding_space_id, text_sha256),
    CONSTRAINT ck_embedding_cache_sha CHECK (text_sha256 ~ '^[0-9a-f]{{64}}$'),
    CONSTRAINT fk_embedding_cache_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
CREATE INDEX ix_embedding_cache_last_used ON embedding_cache (org_id, last_used_at);
ALTER TABLE embedding_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY embedding_cache_all ON embedding_cache FOR ALL
    USING (org_id = {CUR_ORG})
    WITH CHECK (org_id = {CUR_ORG});
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    for role in (
        _role("app_role", "POSTGRES_APP_USER", "ukos_app"),
        _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker"),
    ):
        op.execute(
            f'GRANT SELECT, INSERT, DELETE, UPDATE (last_used_at) ON embedding_cache TO "{role}"'
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS embedding_cache")
