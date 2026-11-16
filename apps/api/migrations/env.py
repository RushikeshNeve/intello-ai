"""Alembic environment (M01.02.01). Online migrations only, as the admin role.

Inputs:
* ``DATABASE_MIGRATION_URL`` (env) or ``-x db_url=...``: admin-role connection. Required.
* ``-x app_role=<name>`` or env ``POSTGRES_APP_USER`` (default ``ukos_app``): the least-privilege
  role that receives table grants. It must differ from the migration user.

The URL is never logged or echoed (it embeds credentials).
"""

from __future__ import annotations

import os
import re

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from app.core.config import to_sqlalchemy_url
from app.core.models import Base

ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

config = context.config
target_metadata = Base.metadata


def _x_args() -> dict[str, str]:
    return context.get_x_argument(as_dictionary=True)


def _migration_url() -> str:
    url = _x_args().get("db_url") or os.environ.get("DATABASE_MIGRATION_URL", "")
    if not url:
        raise SystemExit("DATABASE_MIGRATION_URL is required (admin role; never the app role)")
    return to_sqlalchemy_url(url)


def _app_role() -> str:
    role = _x_args().get("app_role") or os.environ.get("POSTGRES_APP_USER") or "ukos_app"
    if not ROLE_PATTERN.fullmatch(role):
        raise SystemExit("app role name must match ^[a-z_][a-z0-9_]{0,62}$")
    return role


def run_migrations_online() -> None:
    url = _migration_url()
    app_role = _app_role()
    if make_url(url).username == app_role:
        raise SystemExit("refusing to migrate as the app role; use the admin role")
    config.attributes["app_role"] = app_role

    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) mode is not supported; run against a database")
run_migrations_online()
