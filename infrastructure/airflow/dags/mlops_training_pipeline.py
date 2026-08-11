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
3. Three independent quality gates run in **parallel** once ``train``
   succeeds — ``validate_metrics``, ``validate_artifact``,
   ``validate_row_count`` — each a pass/fail check over data ``train``
   already produced (no new I/O). Any one failing leaves
   ``register_and_promote`` ``upstream_failed`` (its trigger rule is
   the Airflow default, ``all_success``) — a candidate that fails a
   sanity check never reaches the promotion endpoint at all.
4. ``register_and_promote`` — calls
   ``POST /api/internal/models/{model_name}/promote`` to create a
   CANDIDATE :class:`ModelVersion`, apply the promotion policy, and
   (on approval) publish an HTTP event to the ServingBridge. The
   framework's own ``TrainingService`` already wrote the TrainingRun
   row; this task only adds the model side.
5. ``report_status`` — always runs (``trigger_rule="all_done"``) and
   tells the framework how the run ended, success or failure.

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


def _import_pipeline(entrypoint: str):
    """Resolve ``"module:callable"`` and return the callable.

    The colon is required here, unlike in ``LocalDockerOrchestrator``.
    Defaulting a bare name to ``<module>:main`` is what let a dag_id be
    mistaken for a training entry point: ``mlops_training_pipeline``
    imported *this module* and then failed looking for ``main`` on it.
    """
    if ":" not in entrypoint:
        raise RuntimeError(
            f"training entrypoint {entrypoint!r} must be 'module:callable'. "
            "TrainingRun.pipeline_id holds the Airflow dag_id here, not the "
            "Python callable — set metadata.training_entrypoint instead."
        )
    module_name, _, fn_name = entrypoint.partition(":")
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, fn_name)


def _resolve_entrypoint(payload: dict[str, Any]) -> str:
    """Find the training callable for a run.

    ``TrainingRun.pipeline_id`` means different things to different
    orchestrators: ``module:callable`` for the local one, a ``dag_id`` for
    this one. So the callable travels in the run's metadata, and the
    top-level field is only a fallback for runs created against the local
    orchestrator's convention.
    """
    meta = payload.get("metadata") or {}
    entrypoint = meta.get("training_entrypoint") or meta.get("pipeline_id")
    if entrypoint:
        return entrypoint
    fallback = payload.get("pipeline_id") or ""
    if ":" in fallback:
        return fallback
    raise RuntimeError(
        "no training entrypoint for this run: set "
        "metadata.training_entrypoint to 'module:callable' when creating "
        f"the TrainingRun (pipeline_id={fallback!r} is the dag_id)."
    )


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

    entrypoint = _resolve_entrypoint(payload)
    fn = _import_pipeline(entrypoint)

    training_config = {
        "training_run_id": payload["training_run_id"],
        "dataset_version_id": payload["dataset_version_id"],
        "csv_uri": payload["storage_uri"],
        # Lets the pipeline confirm the S3 object still hashes to what the
        # dataset version was registered from. .get() rather than [] so an
        # older API that predates the field does not break the DAG.
        "dataset_content_sha256": payload.get("dataset_content_sha256"),
        "tracker_run_id": payload["metadata"].get("tracker_run_id"),
        "tracking_uri": payload["metadata"].get("tracking_uri"),
    }
    # Forward any caller-supplied parameters (e.g. n_estimators).
    training_config.update(payload["metadata"].get("parameters", {}) or {})

    result = fn(training_config)
    if not isinstance(result, dict):
        raise RuntimeError(
            f"pipeline {entrypoint!r} returned {type(result).__name__}, expected a dict"
        )
    if result.get("status") != "SUCCESS":
        raise RuntimeError(
            f"pipeline {entrypoint!r} failed: {result.get('error') or 'unknown'}"
        )
    return {
        "metrics": result.get("metrics", {}),
        "artifact_path": result.get("artifact_path"),
        "params": result.get("params", {}),
        "pipeline": result.get("pipeline"),
    }


def validate_metrics(**context: Any) -> dict[str, Any]:
    """Gate 1/3 — the metrics ``train`` reported are present and sane.

    Independent of ``validate_artifact``/``validate_row_count`` — all
    three run in parallel off the same ``train`` XCom, none reads what
    another writes. Raising here fails only this task; with
    ``register_and_promote``'s default ``all_success`` trigger rule
    that is enough to keep a candidate with a missing or nonsensical
    metric away from the promotion endpoint entirely.
    """
    ti = context["ti"]
    train_payload = ti.xcom_pull(task_ids="train")
    if not train_payload:
        raise RuntimeError("no train XCom to validate")

    metrics = train_payload.get("metrics") or {}
    required = ("f1", "precision", "recall")
    missing = [m for m in required if m not in metrics]
    if missing:
        raise RuntimeError(f"missing required metric(s): {missing}")
    out_of_range = {m: metrics[m] for m in required if not (0.0 <= metrics[m] <= 1.0)}
    if out_of_range:
        raise RuntimeError(f"metric(s) outside [0, 1]: {out_of_range}")
    return {"check": "metrics", "passed": True, "checked": list(required)}


def validate_artifact(**context: Any) -> dict[str, Any]:
    """Gate 2/3 — ``train`` left a real, non-empty file behind.

    ``train`` and this task run in the same Airflow worker
    (``LocalExecutor`` spawns both as subprocesses of the scheduler
    container), so the temp path ``train`` reported is still on the
    same filesystem here — this is not re-reading over the network.
    """
    import os as _os

    ti = context["ti"]
    train_payload = ti.xcom_pull(task_ids="train")
    if not train_payload:
        raise RuntimeError("no train XCom to validate")

    artifact_path = train_payload.get("artifact_path")
    if not artifact_path:
        raise RuntimeError("train() reported no artifact_path")
    if not _os.path.exists(artifact_path):
        raise RuntimeError(f"artifact_path does not exist on disk: {artifact_path}")
    size = _os.path.getsize(artifact_path)
    if size == 0:
        raise RuntimeError(f"artifact at {artifact_path} is empty")
    return {"check": "artifact", "passed": True, "size_bytes": size}


def validate_row_count(**context: Any) -> dict[str, Any]:
    """Gate 3/3 — ``train`` actually trained on the row count the
    DatasetVersion was registered with, not a silently truncated read.
    """
    ti = context["ti"]
    train_payload = ti.xcom_pull(task_ids="train")
    ctx_payload = ti.xcom_pull(task_ids="resolve_context")
    if not train_payload or not ctx_payload:
        raise RuntimeError("missing XCom from upstream tasks")

    trained_rows = (train_payload.get("params") or {}).get("n_rows")
    expected_rows = ctx_payload.get("row_count")
    if trained_rows is None:
        raise RuntimeError("train() reported no params.n_rows")
    if expected_rows is None:
        raise RuntimeError("resolve_context reported no row_count")
    if trained_rows != expected_rows:
        raise RuntimeError(
            f"trained on {trained_rows} rows but DatasetVersion is registered "
            f"with {expected_rows} — pipeline may have read the wrong file"
        )
    return {"check": "row_count", "passed": True, "rows": trained_rows}


def report_status(**context: Any) -> dict[str, Any]:
    """Tell the framework how this DAG run ended.

    Runs with ``trigger_rule="all_done"`` so it fires whether the upstream
    tasks succeeded or failed. Without it a TrainingRun stayed PENDING in
    the framework's database no matter what Airflow did — the DAG had no
    write path back, so the UI could never show a finished run.
    """
    import httpx

    ti = context["ti"]
    ctx_payload = ti.xcom_pull(task_ids="resolve_context")
    if not ctx_payload:
        # resolve_context itself failed; there is no run id to report against.
        print("[airflow] no context payload — nothing to report")
        return {"reported": False}

    run_id = ctx_payload["training_run_id"]
    train_payload = ti.xcom_pull(task_ids="train")

    if train_payload:
        body = {
            "status": "SUCCESS",
            "result": {
                "metrics": train_payload.get("metrics", {}),
                "params": train_payload.get("params", {}),
                "artifact_path": train_payload.get("artifact_path"),
                "pipeline": train_payload.get("pipeline"),
            },
        }
    else:
        failed = [
            t.task_id
            for t in context["dag_run"].get_task_instances()
            if t.state == "failed"
        ]
        body = {
            "status": "FAILED",
            "error_message": (
                f"Airflow tasks failed: {', '.join(failed) or 'unknown'} "
                f"(dag_run {context['dag_run'].run_id})"
            ),
        }

    # A run created by RetrainingWorkflow closes itself out via its own
    # wait_for_completion() poll loop — this call still records
    # metrics/params/artifact_path (register_and_promote and
    # RetrainingWorkflow's own metric resolution both read them back from
    # here), it just must not also transition the run's status, or
    # whichever of the two calls loses the race hits an
    # InvalidStatusTransitionError against an already-terminal row. See
    # _resolve_entrypoint's owned_by_workflow check below for the other
    # half of this.
    owned_by_workflow = bool((ctx_payload.get("metadata") or {}).get("owned_by_workflow"))
    if owned_by_workflow:
        body["skip_lifecycle_transition"] = True

    response = httpx.post(
        f"{APP_BASE_URL}/api/internal/training-runs/{run_id}/finish",
        json=body,
        timeout=30.0,
    )
    response.raise_for_status()
    print(f"[airflow] reported {body['status']} for training run {run_id}")
    return {"reported": True, "status": body["status"]}


def register_and_promote(**context: Any) -> dict[str, Any]:
    """Promote the freshly trained model and reload the serving bridge.

    The promotion policy lives in framework code (called over HTTP,
    see ``mlops_framework.api.routers.internal.promote_model``) —
    Airflow just applies the framework's verdict and, on approval,
    publishes an event so the bridge reloads.

    Skipped entirely when the run's metadata carries "owned_by_workflow"
    (set by RetrainingWorkflow — see workflow/retraining.py): that
    workflow registers and promotes its own ModelVersion in-process after
    this DAG run reaches a terminal state, using its own promotion_config.
    Calling this endpoint too would register a *second*, independent
    CANDIDATE ModelVersion for the same TrainingRun and evaluate it
    against this endpoint's own (looser, min_f1-only) policy instead —
    two different governance decisions racing on the same model.
    """
    import httpx

    ti = context["ti"]
    train_payload = ti.xcom_pull(task_ids="train")
    ctx_payload = ti.xcom_pull(task_ids="resolve_context")
    if not train_payload or not ctx_payload:
        raise RuntimeError("missing XCom from upstream tasks")

    pipeline_meta = ctx_payload.get("metadata") or {}
    if pipeline_meta.get("owned_by_workflow"):
        print(
            "[airflow] owned_by_workflow — skipping register_and_promote, "
            "RetrainingWorkflow handles promotion for this run"
        )
        return {"promoted": False, "skipped": True, "reason": "owned_by_workflow"}

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
    # Three independent quality gates — no ordering between them, so
    # Airflow schedules them in parallel once `train` succeeds.
    validate_metrics_task = PythonOperator(
        task_id="validate_metrics",
        python_callable=validate_metrics,
    )
    validate_artifact_task = PythonOperator(
        task_id="validate_artifact",
        python_callable=validate_artifact,
    )
    validate_row_count_task = PythonOperator(
        task_id="validate_row_count",
        python_callable=validate_row_count,
    )
    # Default trigger rule (all_success): any one gate failing leaves
    # this upstream_failed without ever calling the promote endpoint.
    promote = PythonOperator(
        task_id="register_and_promote",
        python_callable=register_and_promote,
    )
    # all_done: this must run on the failure path too, otherwise a failed
    # training leaves its TrainingRun stuck PENDING in the framework.
    report = PythonOperator(
        task_id="report_status",
        python_callable=report_status,
        trigger_rule="all_done",
    )
    resolve >> train_task >> [validate_metrics_task, validate_artifact_task, validate_row_count_task] >> promote >> report
