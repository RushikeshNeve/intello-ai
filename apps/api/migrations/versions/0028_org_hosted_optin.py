"""M14.05 per-org opt-in for hosted model providers.

``ai_policies.hosted_models_allowed`` (default false): a hosted provider chosen in the AI policy is
refused with ``hosted_not_allowed`` unless an org owner set this. Column only; the existing
``ai_policies`` RLS (members read, owner/admin write) already covers it, and the API narrows the
write of this flag to owners.

Upgrade backfill (integration, behaviour-preserving): orgs whose existing policy already selects a
hosted provider (``allow_external`` and ``openai`` in ``allowed_providers``) get
``hosted_models_allowed = true`` so they keep working after the upgrade. New orgs and every other
existing org default to false.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0028_org_hosted_optin"
down_revision: str | None = "0027_pii_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    op.execute(
        "ALTER TABLE ai_policies ADD COLUMN hosted_models_allowed boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "UPDATE ai_policies SET hosted_models_allowed = true"
        " WHERE allow_external AND 'openai' = ANY(allowed_providers)"
    )
    op.execute(f'GRANT UPDATE (hosted_models_allowed) ON ai_policies TO "{app}"')


def downgrade() -> None:
    op.execute("ALTER TABLE ai_policies DROP COLUMN hosted_models_allowed")
