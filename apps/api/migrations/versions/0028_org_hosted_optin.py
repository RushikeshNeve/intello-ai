"""M14.05 per-org opt-in for hosted model providers.

``ai_policies.hosted_models_allowed`` (default false): a hosted provider chosen in the AI policy is
refused with ``hosted_not_allowed`` unless an org owner set this. Column only; the existing
``ai_policies`` RLS (members read, owner/admin write) already covers it, and the API narrows the
write of this flag to owners.
"""

from __future__ import annotations

from collections.abc import Sequence

import os
import re

from alembic import context, op

revision: str = "0028_org_hosted_optin"
down_revision: str | None = "0019c_shadow_runs"
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
    op.execute(f'GRANT UPDATE (hosted_models_allowed) ON ai_policies TO "{app}"')


def downgrade() -> None:
    op.execute("ALTER TABLE ai_policies DROP COLUMN hosted_models_allowed")
