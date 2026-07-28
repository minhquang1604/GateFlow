# MLflow tracking-server image for the MLOps Framework stack.
#
# The image runs `mlflow server` against:
#   * PostgreSQL on the `postgres` service, database `mlflow`, as the
#     backend store.
#   * MinIO (S3-compatible) on the `minio` service, bucket
#     `mlflow-artifacts`, as the default artifact root.
#
# The entrypoint waits for both dependencies before starting.

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
