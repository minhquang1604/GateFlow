#!/usr/bin/env bash
# Initialize MinIO buckets required by the MLOps stack.
#
# Runs as a one-shot init container. Uses the MinIO `mc` client to
# create the bucket MLflow writes artifacts into.

set -euo pipefail

: "${MINIO_ROOT_USER:=minioadmin}"
: "${MINIO_ROOT_PASSWORD:=minioadmin}"
: "${MINIO_HOST:=minio:9000}"
: "${MLFLOW_BUCKET:=mlflow-artifacts}"

echo "[minio-init] waiting for ${MINIO_HOST} ..."
until curl -fsS "http://${MINIO_HOST}/minio/health/live" >/dev/null 2>&1; do
  sleep 2
done

# Install the official MinIO client (`mc`) into the container.
if ! command -v mc >/dev/null 2>&1; then
  echo "[minio-init] installing mc client ..."
  curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc \
    -o /usr/local/bin/mc
  chmod +x /usr/local/bin/mc
fi

mc alias set local "http://${MINIO_HOST}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >/dev/null

if mc ls "local/${MLFLOW_BUCKET}" >/dev/null 2>&1; then
  echo "[minio-init] bucket '${MLFLOW_BUCKET}' already exists"
else
  echo "[minio-init] creating bucket '${MLFLOW_BUCKET}' ..."
  mc mb "local/${MLFLOW_BUCKET}"
fi

echo "[minio-init] done"
