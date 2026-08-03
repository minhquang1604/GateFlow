# Airflow image for the MLOps Framework.
#
# This image does NOT install the `mlops_framework` package. Airflow
# 2.10.4 pins `SQLAlchemy==1.4.x` internally, and its own ORM models
# use a style SQLAlchemy 2.0 rejects (`MappedAnnotationError`) — the
# framework's own models require `sqlalchemy>=2.0.0`
# (`Mapped`/`mapped_column`). The two cannot coexist in one Python
# environment.
#
# `infrastructure/airflow/dags/mlops_training_pipeline.py` therefore
# talks to the framework over HTTP (`APP_BASE_URL`) instead of
# importing it — see that file's module docstring. The only things
# this image needs are:
#
#   * Airflow itself (from the base image).
#   * The case-study pipeline modules (`case_studies/*`), which have
#     zero `mlops_framework` imports, and their runtime deps (mlflow
#     client, xgboost, scikit-learn, pandas — installed for logging
#     to MLflow directly and running `train_xgboost`).
#   * httpx, for the DAG's calls to the app's internal API and the
#     ServingBridge.
#
# DAGs are bind-mounted (not baked in) so they can be edited without
# rebuilding.

FROM apache/airflow:2.10.4-python3.11

USER root

ENV AIRFLOW_HOME=/opt/airflow \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

USER airflow

WORKDIR /opt/airflow

# Runtime deps required by case_studies/*/pipelines.py and the DAG's
# HTTP calls. Pin versions for reproducible deployments; revisit if
# Airflow breaks these versions.
COPY --chown=airflow:root infrastructure/airflow/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Case study pipeline modules (e.g. case_studies.fraud_detection.
# pipelines) — the DAG's `train` task imports these by module path.
# Copied without the rest of the framework source tree so this image
# never has `mlops_framework` importable.
COPY --chown=airflow:root case_studies /opt/case_studies

ENV PYTHONPATH=/opt/airflow/dags:/opt:${PYTHONPATH:-}

USER root
COPY infrastructure/airflow/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER airflow

ENTRYPOINT ["/entrypoint.sh"]
