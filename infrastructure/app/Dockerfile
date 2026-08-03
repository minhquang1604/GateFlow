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
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/framework
WORKDIR /opt/framework

RUN pip install --no-cache-dir -e .

# The base image sets no PYTHONPATH, so this is the full value
# rather than an append.
ENV PYTHONPATH=/opt/framework/src

EXPOSE 8000 8001
