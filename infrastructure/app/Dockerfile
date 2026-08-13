# Framework image — Management API (`app`) and ServingBridge
# (`serving`). Both ECS services run this same image with different
# `command` overrides (see infrastructure/terraform/environments/prod/
# main.tf), same as docker-compose's `app`/`serving` services.
#
# This image installs `mlops_framework` itself (editable) plus its
# runtime deps declared in pyproject.toml — including
# `sqlalchemy>=2.0.0`, which the framework's ORM models require
# (`Mapped`/`mapped_column`). This is deliberately a *separate* image
# from Airflow's: Airflow 2.10.4 pins `SQLAlchemy==1.4.x` internally
# and cannot tolerate 2.0 in the same environment — see
# infrastructure/airflow/Dockerfile's header comment.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/framework

# Dependencies before source. `pip install -e .` pulls in every package
# in [project.dependencies] — several of them large (xgboost, mlflow,
# scikit-learn, the nvidia CUDA wheels xgboost drags in) — regardless
# of whether the local package's own source changed. With a single
# `COPY . .` ahead of the install, editing so much as one template
# invalidates Docker's cache for that layer and forces a full
# re-download of all of them on every rebuild.
#
# hatchling's default src-layout detection only needs pyproject.toml,
# the README it points at (`readme = "README.md"`), and something at
# src/mlops_framework to resolve the package — not real source — so an
# empty stub is enough to install every dependency and cache that as
# its own layer, independent of source edits.
COPY pyproject.toml README.md ./
RUN mkdir -p src/mlops_framework \
    && touch src/mlops_framework/__init__.py \
    && pip install --no-cache-dir -e . \
    && rm -rf src

COPY . /opt/framework

# Re-run against the real source. Every dependency is already
# installed by the layer above, so this is a fast, network-free step
# that just re-registers the editable package against its actual code.
RUN pip install --no-cache-dir -e .

# The base image sets no PYTHONPATH, so this is the full value
# rather than an append.
ENV PYTHONPATH=/opt/framework/src

EXPOSE 8000 8001
