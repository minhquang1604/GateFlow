#!/usr/bin/env bash
# Airflow container entrypoint for ECS.
#
# docker-compose used a separate one-shot `airflow-common` container
# (depends_on: service_completed_successfully) to run `airflow db
# migrate` + create the admin user before the webserver/scheduler
# started. ECS has no cross-service "wait for completion" primitive,
# so each role folds its own readiness logic in here instead:
#
#   webserver — builds AIRFLOW__DATABASE__SQL_ALCHEMY_CONN from the
#               POSTGRES_* env vars, creates the `airflow` database if
#               it doesn't exist yet (RDS only pre-creates the
#               framework's own initial database), runs the
#               (idempotent) db migrate + admin user create, then
#               execs the webserver. Safe to run on every container
#               start (ECS restarts this on every deploy).
#   scheduler — builds the same connection string, polls
#               `airflow db check` until the webserver's migrate has
#               caught up, then execs the scheduler. Avoids a race
#               where the scheduler starts against a database that
#               hasn't been migrated yet.
#
# Expected environment: POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER,
# POSTGRES_PASSWORD, POSTGRES_DB (defaults to "airflow").
#
# Any other first argument (e.g. `/bin/bash -c "..."`, used by the
# framework's `app`/`serving`/`demo` containers which share this same
# image) is exec'd through unchanged — this entrypoint only intercepts
# the two Airflow roles.
#
# Usage: entrypoint.sh {webserver|scheduler}
#        entrypoint.sh <anything else...>   # passthrough

set -euo pipefail

ROLE="${1:-}"
shift || true

: "${POSTGRES_DB:=airflow}"

_build_connection_string() {
    export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="postgresql+psycopg2://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
}

_ensure_database_exists() {
    python3 - <<PYEOF
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

conn = psycopg2.connect(
    host="${POSTGRES_HOST}",
    port=${POSTGRES_PORT},
    user="${POSTGRES_USER}",
    password="${POSTGRES_PASSWORD}",
    dbname="postgres",
)
conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", ("${POSTGRES_DB}",))
    if cur.fetchone() is None:
        cur.execute('CREATE DATABASE "${POSTGRES_DB}"')
conn.close()
PYEOF
}

case "$ROLE" in
webserver)
    _build_connection_string

    echo "[airflow-entrypoint] ensuring database '${POSTGRES_DB}' exists"
    _ensure_database_exists

    echo "[airflow-entrypoint] running db migrate"
    airflow db migrate

    echo "[airflow-entrypoint] ensuring admin user exists"
    airflow users create \
        --username "${AIRFLOW_ADMIN_USERNAME:-admin}" \
        --password "${AIRFLOW_ADMIN_PASSWORD:?AIRFLOW_ADMIN_PASSWORD is required}" \
        --firstname Admin --lastname User \
        --role Admin --email admin@example.com || true

    echo "[airflow-entrypoint] starting webserver"
    exec airflow webserver "$@"
    ;;
scheduler)
    _build_connection_string

    echo "[airflow-entrypoint] waiting for the metadata database to be migrated"
    for _ in $(seq 1 60); do
        if airflow db check >/dev/null 2>&1; then
            echo "[airflow-entrypoint] database is ready"
            break
        fi
        sleep 5
    done

    echo "[airflow-entrypoint] starting scheduler"
    exec airflow scheduler "$@"
    ;;
*)
    exec "$ROLE" "$@"
    ;;
esac
