# MLflow tracking-server image for the MLOps Framework stack.
#
# The image runs `mlflow server` against:
#   * PostgreSQL as the backend store — the `postgres` service in local
#     docker-compose, or RDS in AWS (MINIO_ENDPOINT unset -> real S3
#     with IAM role credentials).
#   * S3-compatible storage as the artifact root — MinIO locally, the
#     `mlflow-artifacts` S3 bucket (created by the terraform `s3`
#     module) in AWS.
#
# The entrypoint waits for PostgreSQL, creates the backend-store
# database if it doesn't exist yet, then starts the server.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        gcc \
        libpq-dev \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        mlflow==2.20.3 \
        psycopg[binary]==3.2.3 \
        boto3==1.35.36

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
