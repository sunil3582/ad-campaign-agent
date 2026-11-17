#!/bin/sh
set -eu
# Runs once on a new Postgres volume. Application schema migrations run in Python.
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --set app_password="$GUARDIAN_DB_PASSWORD" <<'SQL'
CREATE ROLE guardian LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE guardian OWNER guardian;
CREATE DATABASE guardian_eval OWNER guardian;
SQL

