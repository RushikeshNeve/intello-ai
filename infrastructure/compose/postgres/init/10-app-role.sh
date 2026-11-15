#!/bin/sh
# Creates the least-privilege application role (M00.02.01).
#
# Runs once, on first initialisation of an empty data volume (official postgres entrypoint).
# The role can log in and connect, and may USE schema public, but owns nothing and cannot
# create objects, roles or databases, and cannot bypass row-level security. Table privileges
# are granted later by migrations running as the admin role.
set -eu

: "${POSTGRES_APP_USER:?POSTGRES_APP_USER is required}"
if [ -n "${POSTGRES_APP_PASSWORD_FILE:-}" ]; then
  POSTGRES_APP_PASSWORD="$(cat "$POSTGRES_APP_PASSWORD_FILE")"
fi
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD or POSTGRES_APP_PASSWORD_FILE is required}"

psql -v ON_ERROR_STOP=1 --no-psqlrc \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_user="$POSTGRES_APP_USER" \
  -v app_password="$POSTGRES_APP_PASSWORD" \
  -v db_name="$POSTGRES_DB" <<'EOSQL'
SELECT format(
  'CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION PASSWORD %L',
  :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'app_user') \gexec
-- PG15+ already withholds CREATE on public from PUBLIC; keep it explicit.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user') \gexec
EOSQL
