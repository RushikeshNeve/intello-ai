"""Embedding-cache eviction support: ``embedding_cache_orgs`` lists the orgs that hold cache rows.

The eviction task must visit each org in its own tenant session (RLS), but a worker without org
context cannot enumerate orgs. This definer function returns only org ids (no vectors or hashes),
executable by the worker role. Chained after 0018 to keep a single linear head (the id only names
the file; alembic orders by ``down_revision``).

Revision ID: 0015b_cache_evict_orgs
Revises: 0015_usage_limits
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0015b_cache_evict_orgs"
down_revision: str | None = "0015_usage_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SIGNATURE = "embedding_cache_orgs(integer)"


def _role(key: str, env: str, default: str) -> str:
    value = (
        context.config.attributes.get(key)
        or context.get_x_argument(as_dictionary=True).get(key)
        or os.environ.get(env)
        or default
    )
    if not ROLE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {key} name")
    return str(value)


def upgrade() -> None:
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(
        """
CREATE FUNCTION embedding_cache_orgs(p_limit integer) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT DISTINCT org_id FROM public.embedding_cache ORDER BY org_id LIMIT p_limit
$$;
"""
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO "{worker}"')


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
