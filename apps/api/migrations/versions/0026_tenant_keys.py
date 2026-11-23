"""Per-org data keys for envelope encryption (M14.08).

* ``tenant_keys``: one row per data key (DEK) of an org. ``wrapped_key`` is the DEK wrapped by the
  master key (KEK) named by ``kek_id``; the plaintext DEK is NEVER stored. ``state`` is ``active``
  (at most one per org, partial unique index: seals new values) or ``retired`` (still opens rows
  until ``rotate-keys.sh reencrypt`` moved them). Deleting an org's rows = crypto-shred.
* FORCE RLS by ``org_id = app.current_org_id``. app and worker: SELECT + INSERT (lazy creation of
  an org's first DEK, under the context org). UPDATE/DELETE (KEK rotation, retire, shred) are
  operator-only (admin role, ``python -m app.core.crypto.rotate``).
* worker gains ``UPDATE (token_ciphertext, key_id)`` on connector_accounts: a sync re-seals a
  legacy single-key token under the org's DEK on use (lazy migration; RLS still confines it).

Revision ID: 0026_tenant_keys
Revises: 0025_abuse_canaries
Create Date: 2026-11-22
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0026_tenant_keys"
down_revision: str | None = "0025_abuse_canaries"
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
CREATE TABLE tenant_keys (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    kek_id text NOT NULL,
    wrapped_key bytea NOT NULL,
    state text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    CONSTRAINT pk_tenant_keys PRIMARY KEY (id),
    CONSTRAINT ck_tenant_keys_state CHECK (state IN ('active', 'retired')),
    CONSTRAINT ck_tenant_keys_len CHECK (octet_length(wrapped_key) BETWEEN 40 AND 4096),
    CONSTRAINT fk_tenant_keys_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_tenant_keys_one_active ON tenant_keys (org_id) WHERE state = 'active';
ALTER TABLE tenant_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_keys_all ON tenant_keys FOR ALL
    USING (org_id = {CUR_ORG})
    WITH CHECK (org_id = {CUR_ORG});
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    for role in (app, worker):
        op.execute(f'GRANT SELECT, INSERT ON tenant_keys TO "{role}"')
    op.execute(f'GRANT UPDATE (token_ciphertext, key_id) ON connector_accounts TO "{worker}"')


def downgrade() -> None:
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(f'REVOKE UPDATE (token_ciphertext, key_id) ON connector_accounts FROM "{worker}"')
    op.execute("DROP TABLE IF EXISTS tenant_keys")
