#!/usr/bin/env bash
# Create the extra databases required by the MLOps stack.
#
# Runs once on first Postgres initialization (via the
# docker-entrypoint-initdb.d mechanism). The default
# POSTGRES_DB is created automatically by the entrypoint; we add
# `mlflow` and `airflow` here.

set -e

# Read what's already there. If multiple-databases env was unused,
# parse the comma-separated POSTGRES_MULTIPLE_DATABASES env var.
if [ -n "${POSTGRES_MULTIPLE_DATABASES:-}" ]; then
  for db in $(echo "$POSTGRES_MULTIPLE_DATABASES" | tr ',' ' '); do
    if [ "$db" != "$POSTGRES_DB" ]; then
      echo "[init-databases] creating database '$db'"
      psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" <<-EOSQL
        SELECT 'CREATE DATABASE "$db"' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
EOSQL
    fi
  done
fi
