# Airflow image for the MLOps Framework.
#
# The DAGs mounted into this image live in
# `infrastructure/airflow/dags/` and execute inside Airflow workers.
# The image installs:
#
#   * The framework itself, in editable mode from `/opt/framework`.
#   * Heavy ML runtime deps that Airflow tasks need (mlflow, xgboost,
#     scikit-learn, pandas).
#
# The framework source tree is copied into the image at build time so
# that production deployments do not need a bind-mount for it. DAGs are
# bind-mounted so they can be edited without rebuilding.

FROM apache/airflow:2.10.4-python3.11

USER root

ENV AIRFLOW_HOME=/opt/airflow \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build-time: install build tooling required by some wheels (xgboost
# is a pre-built wheel but pinning tools avoids surprises when adding
# new deps).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the framework sources and install in editable mode. We can't
# do this as the airflow user because /opt/framework needs to be
# owned by it later; install as root then chown.
COPY --chown=airflow:root . /opt/framework

USER airflow

WORKDIR /opt/airflow

# Install the framework's own dependency contract (pyproject.toml)
# first, in editable mode — this is what actually pulls in
# sqlalchemy>=2.0.0 and overrides Airflow's bundled SQLAlchemy 1.4.x
# (which lacks `mapped_column` and breaks mlops_framework.database
# imports). Then install the runtime deps required by DAGs. Pin
# versions for reproducible deployments; revisit if Airflow breaks
# these versions.
RUN pip install --no-cache-dir -e /opt/framework \
    && pip install --no-cache-dir \
        -r /opt/framework/infrastructure/airflow/requirements.txt

WORKDIR /opt/framework

# Make sure both airflow modules and the framework are importable.
ENV PYTHONPATH=/opt/airflow/dags:/opt/framework/src:${PYTHONPATH:-}

USER root
COPY infrastructure/airflow/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER airflow

ENTRYPOINT ["/entrypoint.sh"]
