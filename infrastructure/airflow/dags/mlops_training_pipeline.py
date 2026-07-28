"""Real Airflow DAG for the MLOps Framework.

This DAG is the production counterpart of the framework's
``LocalDockerOrchestrator``. The framework itself never imports
``airflow``; this file is the only place the two systems meet.

Flow
----
1. ``resolve_context`` — opens a framework session, fetches the
   :class:`TrainingRun` + :class:`DatasetVersion` referenced by
   ``dag_run.conf``, and returns the data XCom tasks need.
2. ``train`` — invokes the pipeline registered as
   ``conf['pipeline_id']`` (a ``module:callable`` identifier). Logs
   params + metrics to MLflow when a tracker run id is supplied.
3. ``register_and_promote`` — creates a :class:`ModelVersion` in the
   CANDIDATE state, applies the promotion policy, and (on approval)
   publishes an HTTP event to the ServingBridge. The framework's own
   ``TrainingService`` already wrote the TrainingRun row; this task
   only adds the model side.

The DAG runs on Airflow 2.x. It is intentionally minimal — there is
no SLA, no retries, and no scheduling. The framework controls all
governance; Airflow is just the executor.
"""

from __future__ import annotations

import json
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
    """Pull the framework-side TrainingRun + DatasetVersion.

    Reads ``training_run_id`` from ``dag_run.conf`` and surfaces a
    plain dict through XCom. Anything that talks to the framework's
    database goes through this task so the others stay pure-Python.
    """
    from sqlalchemy.orm import Session

    from mlops_framework.config.settings import get_settings
    from mlops_framework.database.session import DatabaseManager
    from mlops_framework.database.models.dataset_version import DatasetVersion
    from mlops_framework.database.models.training_run import TrainingRun

    settings = get_settings()
    db = DatabaseManager(database_url=settings.database_url)
    session: Session = db.session_factory()
    try:
        conf = (context.get("dag_run") or {}).conf or {}
        run_id = int(conf["training_run_id"])
        run = session.get(TrainingRun, run_id)
        if run is None:
            raise RuntimeError(f"TrainingRun {run_id} not found")
        dataset_version = session.get(DatasetVersion, run.dataset_version_id)
        if dataset_version is None:
            raise RuntimeError(
                f"DatasetVersion {run.dataset_version_id} not found"
            )
        payload = {
            "training_run_id": run.id,
            "dataset_version_id": dataset_version.id,
            "storage_uri": dataset_version.storage_uri,
            "row_count": dataset_version.row_count,
            "pipeline_id": run.pipeline_id,
            "metadata": json.loads(run.metadata_json or "{}"),
        }
        return payload
    finally:
        session.close()


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

    The promotion policy lives in framework code — Airflow just applies
    the framework's verdict. On approval we publish an
    :class:`HttpEventPublisher` payload so the bridge reloads.
    """
    import httpx

    from mlops_framework.config.settings import get_settings
    from mlops_framework.database.session import DatabaseManager
    from mlops_framework.database.models.model import Model as ModelRow
    from mlops_framework.database.models.model_version import (
        ModelState,
        ModelVersion,
    )
    from mlops_framework.database.models.training_run import TrainingRun
    from mlops_framework.exceptions import ModelNotFoundError
    from mlops_framework.governance.promotion import (
        ModelPromotionPolicy,
        PromotionConfig,
        PromotionContext,
    )
    from mlops_framework.model.manager import ModelManager

    settings = get_settings()
    db = DatabaseManager(database_url=settings.database_url)
    session = db.session_factory()
    try:
        ti = context["ti"]
        train_payload = ti.xcom_pull(task_ids="train")
        ctx_payload = ti.xcom_pull(task_ids="resolve_context")
        if not train_payload or not ctx_payload:
            raise RuntimeError("missing XCom from upstream tasks")

        run = session.get(TrainingRun, ctx_payload["training_run_id"])
        if run is None:
            raise RuntimeError(
                f"TrainingRun {ctx_payload['training_run_id']} not found"
            )
        pipeline_meta = json.loads(run.metadata_json or "{}")
        model_name = pipeline_meta.get("model_name", "fraud-xgboost")
        tracker_run_id = pipeline_meta.get("tracker_run_id")

        # The model row should already exist (created by the demo /
        # app code). Fail loudly otherwise — the DAG can't create it
        # without knowing the framework's full set of fields.
        mm = ModelManager(session)
        model_row = mm.get_model_by_name(model_name)
        if model_row is None:
            raise ModelNotFoundError(f"Model {model_name!r} not registered")

        candidate = mm.create_model_version(
            model_id=model_row.id,
            dataset_version_id=ctx_payload["dataset_version_id"],
            training_run_id=ctx_payload["training_run_id"],
            mlflow_run_id=tracker_run_id,
            state=ModelState.CANDIDATE,
            metrics=train_payload["metrics"],
            artifact_uri=train_payload["artifact_path"],
        )

        production = mm._production_for_model(model_row.id)  # noqa: SLF001
        decision = ModelPromotionPolicy().evaluate(
            context=PromotionContext(candidate=candidate, production=production),
            config=PromotionConfig(
                min_metrics={"f1": pipeline_meta.get("min_f1", 0.0)},
                must_beat_production=False,
                allow_cold_start=True,
            ),
        )
        if not decision.approved:
            mm.transition_state(candidate.id, ModelState.REJECTED)
            return {
                "promoted": False,
                "model_version_id": candidate.id,
                "reasons": decision.reasons,
            }

        mm.transition_state(candidate.id, ModelState.APPROVED)
        mm.transition_state(candidate.id, ModelState.PRODUCTION)
        if production is not None and production.id != candidate.id:
            mm.transition_state(production.id, ModelState.ARCHIVED)

        # Emit a promotion event so the serving bridge reloads.
        if settings.serving_bridge_url:
            try:
                httpx.post(
                    f"{settings.serving_bridge_url}/internal/model/reload",
                    json={
                        "model_name": model_row.name,
                        "model_version": candidate.version_number,
                        "artifact_uri": candidate.artifact_uri,
                        "metrics": train_payload["metrics"],
                        "model_id": candidate.model_id,
                        "model_version_id": candidate.id,
                    },
                    timeout=10.0,
                )
            except Exception as exc:  # pragma: no cover - env dependent
                print(f"[airflow] reload POST failed: {exc}")

        return {
            "promoted": True,
            "model_version_id": candidate.id,
            "model_version": candidate.version_number,
        }
    finally:
        session.close()


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