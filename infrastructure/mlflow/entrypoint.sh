#!/usr/bin/env bash
# Boot script for the MLflow tracking server.
#
# Waits for PostgreSQL and MinIO before starting the server so cold
# reboots don't race against the dependencies.

set -euo pipefail

: "${POSTGRES_HOST:=postgres}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:=postgres}"
: "${POSTGRES_PASSWORD:=postgres}"
: "${POSTGRES_DB:=mlflow}"
: "${MINIO_ENDPOINT:=http://minio:9000}"
: "${MINIO_ACCESS_KEY:=minioadmin}"
: "${MINIO_SECRET_KEY:=minioadmin}"
: "${MLFLOW_BUCKET:=mlflow-artifacts}"
: "${MLFLOW_HOST:=0.0.0.0}"
: "${MLFLOW_PORT:=5000}"

echo "[mlflow] waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT} ..."
for _ in $(seq 1 60); do
  if nc -z "${POSTGRES_HOST}" "${POSTGRES_PORT}" 2>/dev/null; then
    echo "[mlflow] PostgreSQL is up"
    break
  fi
  sleep 2
done

echo "[mlflow] waiting for MinIO at ${MINIO_ENDPOINT} ..."
for _ in $(seq 1 60); do
  if curl -fsS "${MINIO_ENDPOINT}/minio/health/live" >/dev/null 2>&1; then
    echo "[mlflow] MinIO is up"
    break
  fi
  sleep 2
done

# Export the S3 endpoint and credentials so the `mlflow` CLI inside
# this container reaches MinIO (no AWS).
export MLFLOW_S3_ENDPOINT_URL="${MINIO_ENDPOINT}"
export AWS_ACCESS_KEY_ID="${MINIO_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${MINIO_SECRET_KEY}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

BACKEND_URI="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
ARTIFACT_ROOT="s3://${MLFLOW_BUCKET}"

echo "[mlflow] starting tracking server..."
echo "[mlflow]   backend-store-uri : ${BACKEND_URI}"
echo "[mlflow]   default-artifact-root: ${ARTIFACT_ROOT}"

exec mlflow server \
  --backend-store-uri "${BACKEND_URI}" \
  --default-artifact-root "${ARTIFACT_ROOT}" \
  --host "${MLFLOW_HOST}" \
  --port "${MLFLOW_PORT}"
