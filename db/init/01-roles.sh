#!/bin/sh
# Runs once, when the Postgres volume is first created (docker-entrypoint-initdb.d).
#
# Three login roles:
#   consultant_owner  owns every schema and table; runs the migrations. Owners bypass
#                     row-level security, so the app never connects as this role.
#   web_user          the web app and its worker: Workspace, Chat, Eval Lab, Self-Improve, Go Live.
#                     It gets NO access to the brain schema, so those features can only reach
#                     the Knowledge Brain through its API, as the brief requires.
#   brain_user        the Knowledge Brain's API and worker.
# Grants and row-level security policies are in the migrations, next to the tables.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v owner_pw="$OWNER_DB_PASSWORD" -v web_pw="$WEB_DB_PASSWORD" -v brain_pw="$BRAIN_DB_PASSWORD" <<'EOSQL'
CREATE ROLE consultant_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE web_user LOGIN PASSWORD :'web_pw';
CREATE ROLE brain_user LOGIN PASSWORD :'brain_pw';
CREATE DATABASE consultant_test OWNER consultant_owner;
ALTER DATABASE consultant OWNER TO consultant_owner;
EOSQL

# Extensions need a superuser; schemas belong to the owner. Same in the test database.
for db in "$POSTGRES_DB" consultant_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
ALTER SCHEMA public OWNER TO consultant_owner;
GRANT USAGE ON SCHEMA public TO web_user, brain_user;
EOSQL
done
