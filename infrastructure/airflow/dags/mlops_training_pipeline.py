"""Real Airflow DAG for the MLOps Framework.

This DAG is the production counterpart of the framework's
``LocalDockerOrchestrator``. It talks to the framework over HTTP
(``APP_BASE_URL``), never by importing ``mlops_framework`` directly.

Why HTTP and not a direct import: Airflow 2.10.4 pins
``SQLAlchemy==1.4.x`` internally, and its own ORM models are written
in a style SQLAlchemy 2.0 rejects (``MappedAnnotationError``). The
framework's own models require ``sqlalchemy>=2.0.0``
(``Mapped``/``mapped_column``). The two cannot coexist in one Python
environment, so the Airflow image
(``infrastructure/airflow/Dockerfile``) does not install the
framework package at all — this DAG is the only place the two
systems meet, and it meets over the app's HTTP API
(``mlops_framework.api.routers.internal``), not in-process.

Flow
----
1. ``resolve_context`` — calls ``GET /api/internal/training-runs/{id}/context``
   to fetch the TrainingRun + DatasetVersion this run needs, and
   returns the data XCom tasks need.
2. ``train`` — invokes the pipeline registered as
   ``conf['pipeline_id']`` (a ``module:callable`` identifier). Case
   study pipeline modules (e.g. ``case_studies.fraud_detection.
   pipelines``) have no framework dependency, so they import fine in
   this environment. Logs params + metrics to MLflow directly when a
   tracker run id is supplied.
3. ``register_and_promote`` — calls
   ``POST /api/internal/models/{model_name}/promote`` to create a
   CANDIDATE :class:`ModelVersion`, apply the promotion policy, and
   (on approval) publish an HTTP event to the ServingBridge. The
   framework's own ``TrainingService`` already wrote the TrainingRun
   row; this task only adds the model side.

The DAG runs on Airflow 2.x. It is intentionally minimal — there is
no SLA, no retries, and no scheduling. The framework controls all
governance; Airflow is just the executor.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator

DEFAULT_ARGS = {
    "owner": "mlops-framework",
    "depends_on_past": False,
    "retries": 0,
    "retry_delay": timedelta(minutes=1),
    "execution_timeout": timedelta(minutes=30),
}

# ECS Service Connect discovery name (bare hostname, no namespace
# suffix) — see infrastructure/terraform/environments/prod/main.tf.
# Falls back to the docker-compose service name for local dev.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://app:8000")
SERVING_BRIDGE_URL = os.environ.get("SERVING_BRIDGE_URL", "http://serving:8001")


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _import_pipeline(pipeline_id: str):
    """Resolve ``"module:callable"`` and return the callable."""
    if ":" in pipeline_id:
        module_name, _, fn_name = pipeline_id.partition(":")
    else:
        module_name, fn_name = pipeline_id, "main"
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, fn_name)


# ---------------------------------------------------------------------- #
# Tasks
# ---------------------------------------------------------------------- #


def resolve_context(**context: Any) -> dict[str, Any]:
    """Fetch the framework-side TrainingRun + DatasetVersion over HTTP.

    Reads ``training_run_id`` from ``dag_run.conf`` and surfaces the
    app's response through XCom.
    """
    import httpx

    conf = (context.get("dag_run") or {}).conf or {}
    run_id = int(conf["training_run_id"])

    response = httpx.get(
        f"{APP_BASE_URL}/api/internal/training-runs/{run_id}/context",
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


def train(**context: Any) -> dict[str, Any]:
    """Execute the registered pipeline and log to MLflow.

    The pipeline callable is invoked with the same contract that
    :class:`LocalDockerOrchestrator` uses (``module:callable(config)``).
    This means application developers can register pipelines once and
    have them run locally or in Airflow without code change.
    """
    ti = context["ti"]
    payload = ti.xcom_pull(task_ids="resolve_context")
    if not payload:
        raise RuntimeError("resolve_context did not return a payload")

    pipeline_id = payload["pipeline_id"]
    fn = _import_pipeline(pipeline_id)

    training_config = {
        "training_run_id": payload["training_run_id"],
        "dataset_version_id": payload["dataset_version_id"],
        "csv_uri": payload["storage_uri"],
        "tracker_run_id": payload["metadata"].get("tracker_run_id"),
        "tracking_uri": payload["metadata"].get("tracking_uri"),
    }
    # Forward any caller-supplied parameters (e.g. n_estimators).
    training_config.update(payload["metadata"].get("parameters", {}) or {})

    result = fn(training_config)
    if result.get("status") != "SUCCESS":
        raise RuntimeError(
            f"pipeline {pipeline_id!r} failed: {result.get('error') or 'unknown'}"
        )
    return {
        "metrics": result.get("metrics", {}),
        "artifact_path": result.get("artifact_path"),
        "params": result.get("params", {}),
        "pipeline": result.get("pipeline"),
    }


def register_and_promote(**context: Any) -> dict[str, Any]:
    """Promote the freshly trained model and reload the serving bridge.

    The promotion policy lives in framework code (called over HTTP,
    see ``mlops_framework.api.routers.internal.promote_model``) —
    Airflow just applies the framework's verdict and, on approval,
    publishes an event so the bridge reloads.
    """
    import httpx

    ti = context["ti"]
    train_payload = ti.xcom_pull(task_ids="train")
    ctx_payload = ti.xcom_pull(task_ids="resolve_context")
    if not train_payload or not ctx_payload:
        raise RuntimeError("missing XCom from upstream tasks")

    conf = (context.get("dag_run") or {}).conf or {}
    pipeline_meta = ctx_payload.get("metadata") or {}
    model_name = pipeline_meta.get("model_name", "fraud-xgboost")
    tracker_run_id = pipeline_meta.get("tracker_run_id")

    response = httpx.post(
        f"{APP_BASE_URL}/api/internal/models/{model_name}/promote",
        json={
            "dataset_version_id": ctx_payload["dataset_version_id"],
            "training_run_id": ctx_payload["training_run_id"],
            "mlflow_run_id": tracker_run_id,
            "metrics": train_payload["metrics"],
            "artifact_uri": train_payload["artifact_path"],
            "min_f1": pipeline_meta.get("min_f1", 0.0),
        },
        timeout=30.0,
    )
    response.raise_for_status()
    result = response.json()

    if result["promoted"] and SERVING_BRIDGE_URL:
        try:
            httpx.post(
                f"{SERVING_BRIDGE_URL}/internal/model/reload",
                json={
                    "model_name": model_name,
                    "model_version": result["model_version"],
                    "artifact_uri": train_payload["artifact_path"],
                    "metrics": train_payload["metrics"],
                    "model_version_id": result["model_version_id"],
                },
                timeout=10.0,
            )
        except Exception as exc:  # pragma: no cover - env dependent
            print(f"[airflow] reload POST failed: {exc}")

    return result


# ---------------------------------------------------------------------- #
# DAG
# ---------------------------------------------------------------------- #


with DAG(
    dag_id="mlops_training_pipeline",
    default_args=DEFAULT_ARGS,
    description="MLOps Framework training pipeline (Airflow executor)",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["mlops", "framework"],
) as dag:
    resolve = PythonOperator(
        task_id="resolve_context",
        python_callable=resolve_context,
    )
    train_task = PythonOperator(
        task_id="train",
        python_callable=train,
    )
    promote = PythonOperator(
        task_id="register_and_promote",
        python_callable=register_and_promote,
    )
    resolve >> train_task >> promote
