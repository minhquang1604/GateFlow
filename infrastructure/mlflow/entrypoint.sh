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
: "${MLFLOW_BUCKET:=mlflow-artifacts}"
: "${MLFLOW_HOST:=0.0.0.0}"
: "${MLFLOW_PORT:=5000}"
# Optional MinIO compatibility — leave empty on AWS so the IAM role
# provides S3 credentials and we hit the real S3 endpoint.
: "${MINIO_ENDPOINT:=}"
: "${MINIO_ACCESS_KEY:=}"
: "${MINIO_SECRET_KEY:=}"

echo "[mlflow] waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT} ..."
for _ in $(seq 1 60); do
  if nc -z "${POSTGRES_HOST}" "${POSTGRES_PORT}" 2>/dev/null; then
    echo "[mlflow] PostgreSQL is up"
    break
  fi
  sleep 2
done

if [ -n "${MINIO_ENDPOINT}" ]; then
  echo "[mlflow] waiting for MinIO at ${MINIO_ENDPOINT} ..."
  for _ in $(seq 1 60); do
    if curl -fsS "${MINIO_ENDPOINT}/minio/health/live" >/dev/null 2>&1; then
      echo "[mlflow] MinIO is up"
      break
    fi
    sleep 2
  done

  # Point the MLflow CLI at the local MinIO endpoint.
  export MLFLOW_S3_ENDPOINT_URL="${MINIO_ENDPOINT}"
  export AWS_ACCESS_KEY_ID="${MINIO_ACCESS_KEY}"
  export AWS_SECRET_ACCESS_KEY="${MINIO_SECRET_KEY}"
else
  echo "[mlflow] MINIO_ENDPOINT unset — using AWS S3 with IAM role credentials"
  # Make sure we don't accidentally inherit MinIO env from the host
  # shell or a stale container env.
  unset MLFLOW_S3_ENDPOINT_URL
fi

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
