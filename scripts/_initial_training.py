"""Shared "train to SUCCESS via the real Airflow DAG, then promote" flow.

Both demo scripts drive the exact same governance chain for their first
model version:

    Dataset -> DatasetVersion (content-hash pinned, verified by the
      Airflow task before it trains on it)
        -> ReadinessEngine -> TrainingEligibilityPolicy
            -> AirflowOrchestrator triggers the real
               ``mlops_training_pipeline`` DAG
                -> train_xgboost (real XGBoost, real MLflow run,
                   content-hash checked)
            -> TrainingRun SUCCESS -> ModelVersion CANDIDATE
                -> ModelPromotionPolicy (cold start) -> PRODUCTION
                    -> real ServingBridge reload event

It is deliberately verbose — dataset profile, readiness checks per
check, eligibility reasons, per-task Airflow states, every metric —
because the point of "training" in this demo suite is to leave a rich
trail to browse afterwards in the Management UI ("Gateflow"): the run
detail page, the DAG's Graph View + task history, the model's metrics,
the lineage graph.

``run_end_to_end_demo.py`` (train-once demo) and
``run_drift_recovery_demo.py`` (train + drift + retrain demo)'s Phase 1
both call :func:`run_initial_training` instead of hand-rolling their
own copy of this — they only differ in *which* dataset/model names,
CSV generation params, and training hyperparameters they pass in.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy import select

from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.database.session import DatabaseManager
from mlops_framework.dataset.checksum import calculate_file_checksum
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.governance.eligibility import (
    EligibilityConfig,
    TrainingEligibilityPolicy,
)
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.airflow import AirflowOrchestrator
from mlops_framework.readiness.engine import ReadinessEngine, TrainingPolicy
from mlops_framework.tracking.mlflow import MLflowTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService

from case_studies.fraud_detection import data as fraud_data

STEP_SEP = "─" * 72


def _print_banner(title: str) -> None:
    print(f"\n{STEP_SEP}\n  {title}\n{STEP_SEP}")


def _detail(message: str) -> None:
    print(f"  • {message}", flush=True)


def _wait_for(url: str, *, label: str, timeout: float = 180.0) -> None:
    """Poll ``url`` until it returns 2xx/3xx or we time out."""
    print(f"  waiting for {label} at {url} ...", flush=True)
    deadline = time.time() + timeout
    last_err: Optional[str] = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code < 500:
                print(f"  ✓ {label} is up ({response.status_code})")
                return
        except Exception as exc:
            last_err = repr(exc)
        time.sleep(2.0)
    raise RuntimeError(
        f"{label} at {url} did not become ready within {timeout:.0f}s "
        f"(last error: {last_err})"
    )


def resolve_endpoints(settings: Any) -> dict[str, str]:
    """Resolve the service URLs the demo scripts talk to.

    Honors explicit env vars first (Docker), falls back to the
    ``Settings`` instance (which itself reads the same env vars for
    out-of-container runs).
    """
    import os

    return {
        "mlflow_uri": os.environ.get("MLFLOW_TRACKING_URI")
        or settings.mlflow_tracking_uri
        or "http://localhost:5000",
        "airflow_url": os.environ.get("AIRFLOW_BASE_URL")
        or settings.airflow_base_url
        or "http://localhost:8080",
        "serving_url": os.environ.get("SERVING_BRIDGE_URL")
        or settings.serving_bridge_url
        or "http://localhost:8001",
        # The Airflow `train` task always executes inside the
        # airflow-scheduler container (LocalExecutor), on the docker
        # network — so the tracking_uri handed to *it* (via TrainingRun
        # metadata) must be the in-network service name, regardless of
        # what URL *this* process used above to reach MLflow (localhost
        # when run from the host, `mlflow` when run inside the network).
        # Conflating the two makes the DAG's train task try to reach
        # "localhost:5000" from inside its own container and silently
        # skip all MLflow logging (params, metrics, artifact).
        "mlflow_uri_for_airflow": os.environ.get(
            "AIRFLOW_INTERNAL_MLFLOW_URI", "http://mlflow:5000"
        ),
    }


def default_training_policy() -> TrainingPolicy:
    return TrainingPolicy(
        required_size=1000,
        freshness_hours=24 * 365,
        required_columns=fraud_data.feature_columns() + [fraud_data.target_column()],
        expected_column_count=31,
        max_missing_ratio=0.0,
    )


@dataclass
class InitialTrainingResult:
    """Everything a caller needs to keep going (drift injection, live
    re-scoring, lineage) after the model has been trained + promoted."""

    dataset_id: int
    dataset_version_id: int
    model_id: int
    model_version_id: int
    model_version_number: int
    model_state: str
    metrics: dict[str, Any]
    promoted: bool
    tracker_run_id: Optional[str]
    mlflow_run_id: Optional[str]
    execution_id: str
    dag_id: str
    content_sha256: str
    csv_path: Path


def run_initial_training(
    db: DatabaseManager,
    endpoints: dict[str, str],
    settings: Any,
    *,
    dataset_name: str,
    dataset_description: str,
    model_name: str,
    model_description: str,
    pipeline_friendly: str,
    pipeline_id: str,
    dag_id: str,
    experiment_name: str,
    csv_local_path: Path,
    airflow_csv_path: str,
    csv_write_kwargs: Optional[dict[str, Any]] = None,
    training_params: Optional[dict[str, Any]] = None,
    min_f1: float = 0.5,
    training_policy: Optional[TrainingPolicy] = None,
    timeout: float = 600.0,
    step_prefix: str = "",
) -> InitialTrainingResult:
    """Train a fresh model to SUCCESS via the real Airflow DAG and
    return everything downstream steps need. Raises ``SystemExit`` with
    an explanatory message on any blocking failure (readiness, DAG
    failure, promotion not applied) — callers running this as their
    whole program can let it propagate; callers embedding it in a
    longer story (e.g. Phase 1 of the drift demo) should let it
    propagate too, since there is nothing sensible to continue with.
    """
    csv_write_kwargs = csv_write_kwargs or {}
    training_params = training_params or {"max_depth": 6, "n_estimators": 150, "learning_rate": 0.1}
    training_policy = training_policy or default_training_policy()

    def step(n: int, total: int, title: str) -> None:
        label = f"{step_prefix} {n}/{total}  {title}" if step_prefix else f"{n}/{total}  {title}"
        _print_banner(label)

    # ------------------------------------------------------------------ #
    # 1. Dataset + DatasetVersion (content-hash pinned)
    # ------------------------------------------------------------------ #
    step(1, 6, "Dataset + DatasetVersion (content-hash pinned)")
    csv_local_path.parent.mkdir(parents=True, exist_ok=True)
    fraud_data.write_csv(csv_local_path, **csv_write_kwargs)
    profile = fraud_data.describe_csv(csv_local_path)
    content_sha256 = calculate_file_checksum(csv_local_path)
    metadata = dict(profile["metadata"])
    metadata["content_sha256"] = content_sha256
    metadata["size_bytes"] = csv_local_path.stat().st_size
    _detail(
        f"wrote {csv_local_path.name} — {profile['row_count']:,} rows, "
        f"{metadata['n_fraud']} fraud ({metadata['fraud_ratio']:.4%})"
    )
    _detail(f"content_sha256={content_sha256[:16]}… — the Airflow task verifies this before training")

    with db.get_session() as session:
        dm = DatasetManager(session)
        dataset = dm.get_dataset_by_name(dataset_name)
        if dataset is None:
            dataset = dm.create_dataset(dataset_name, description=dataset_description)
            _detail(f"created dataset {dataset.name!r} (id={dataset.id})")
        else:
            _detail(f"dataset {dataset.name!r} already exists (id={dataset.id}) — reusing")

        existing = [
            v for v in dm.list_versions(dataset.id)
            if json.loads(v.metadata_json or "{}").get("content_sha256") == content_sha256
        ]
        if existing:
            version = existing[-1]
            _detail(f"identical content already registered as v{version.version_number}")
        else:
            version = dm.create_version(
                dataset_id=dataset.id,
                storage_uri=airflow_csv_path,
                row_count=profile["row_count"],
                metadata=metadata,
            )
            _detail(
                f"registered DatasetVersion v{version.version_number} (id={version.id}) "
                f"— storage_uri (Airflow-side) = {airflow_csv_path}"
            )
        session.commit()
        dataset_id, version_id = dataset.id, version.id

    # ------------------------------------------------------------------ #
    # 2. Readiness
    # ------------------------------------------------------------------ #
    step(2, 6, "ReadinessEngine")
    with db.get_session() as session:
        version_row = session.get(DatasetVersion, version_id)
        readiness = ReadinessEngine(session).evaluate(version_row, policy=training_policy)
        for name, outcome in readiness.check_dict().items():
            _detail(f"{name:20} {outcome}")
        _detail(f"=> {readiness.status.value}")
        if not readiness.is_ready:
            raise SystemExit(f"dataset BLOCKED: {readiness.reasons}")

    # ------------------------------------------------------------------ #
    # 3. Model + Eligibility
    # ------------------------------------------------------------------ #
    step(3, 6, "Model + TrainingEligibilityPolicy")
    with db.get_session() as session:
        mm = ModelManager(session)
        model_row = mm.get_model_by_name(model_name)
        if model_row is None:
            model_row = mm.create_model(
                model_name, task="binary_classification", description=model_description
            )
            _detail(f"created model {model_name!r} (id={model_row.id})")
        else:
            _detail(f"model {model_name!r} already exists (id={model_row.id}) — reusing")
        session.commit()
        model_id = model_row.id

    with db.get_session() as session:
        version_row = session.get(DatasetVersion, version_id)
        model_row = session.get(ModelRow, model_id)
        ctx = TrainingEligibilityPolicy(session).build_context(
            dataset_version=version_row, readiness=readiness, model=model_row, force=True,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(ctx, EligibilityConfig())
        _detail(f"eligible={decision.eligible} reasons={decision.reasons}")

    # ------------------------------------------------------------------ #
    # 4. Training — real Airflow DAG
    # ------------------------------------------------------------------ #
    step(4, 6, "Training — real Airflow DAG → train_xgboost")
    tracker = MLflowTracker(tracking_uri=endpoints["mlflow_uri"], experiment_name=experiment_name)
    # Plain construct + duck-typed close in `finally` rather than `with
    # AirflowOrchestrator(...) as orchestrator:` — callers/tests may
    # substitute a different Orchestrator implementation here (e.g.
    # LocalDockerOrchestrator) that doesn't implement the context-manager
    # protocol, and requiring it would make this function harder to
    # exercise hermetically than the rest of the framework's API.
    orchestrator = AirflowOrchestrator(
        base_url=endpoints["airflow_url"],
        username=settings.airflow_username or "airflow",
        password=settings.airflow_password or "airflow",
    )
    try:
        with db.get_session() as session:
            dm = DatasetManager(session)
            tm = TrainingManager(session, dm)
            service = TrainingService(training_manager=tm, orchestrator=orchestrator, tracker=tracker)
            run = service.create_run(
                dataset_version_id=version_id,
                pipeline_id=dag_id,
                trigger_type="MANUAL",
                metadata={
                    "training_entrypoint": pipeline_id,
                    "parameters": training_params,
                    "csv_uri": airflow_csv_path,
                    "model_name": model_name,
                    "min_f1": min_f1,
                    "tracking_uri": endpoints["mlflow_uri_for_airflow"],
                },
            )
            run_id = run.id
            _detail(f"TrainingRun created (id={run_id})")

            tracker_run_id = tracker.start_run(
                run_name=f"training-run-{run_id}",
                tags={"training_run_id": str(run_id), "pipeline_id": pipeline_id},
            )
            tracker.log_params(
                {"pipeline": pipeline_friendly, "csv_uri": airflow_csv_path, **csv_write_kwargs}
            )
            _detail(f"MLflow run started (id={tracker_run_id})")
            tm.update_metadata(
                run_id,
                {
                    "tracker_run_id": tracker_run_id,
                    "tracking_uri": endpoints["mlflow_uri_for_airflow"],
                    "orchestrator_execution_id": "pending",
                },
            )
            execution_id = service.start_run(run_id)
            tm.update_metadata(run_id, {"orchestrator_execution_id": execution_id})
            _detail(f"triggered Airflow DAG run: {execution_id}")

        # -------------------------------------------------------------- #
        # 5. Wait for the Airflow DAG run to reach SUCCESS
        # -------------------------------------------------------------- #
        step(5, 6, "Waiting for the Airflow DAG run to finish")
        deadline = time.time() + timeout
        final_state = "UNKNOWN"
        while time.time() < deadline:
            status = orchestrator.get_execution_status(execution_id)
            # Per-task detail is an Airflow-specific extra, not part of
            # the generic Orchestrator interface — duck-type it so a
            # substitute (e.g. LocalDockerOrchestrator, used in tests
            # and by Phase 2 of the drift demo) still works, just
            # without the task-by-task breakdown.
            get_task_instances = getattr(orchestrator, "get_task_instances", None)
            tasks = get_task_instances(execution_id) if get_task_instances else None
            task_summary = (
                ", ".join(f"{t['task_id']}={t['state']}" for t in tasks) if tasks else "(scheduling…)"
            )
            _detail(f"{status.state.value:<10} {task_summary}")
            if status.state.value in ("SUCCESS", "FAILED", "CANCELLED"):
                final_state = status.state.value
                break
            time.sleep(3.0)

        if final_state != "SUCCESS":
            tracker.end_run(status="FAILED")
            raise SystemExit(f"Airflow DAG run did not succeed (final={final_state})")

        # The DAG's own `report_status` task already calls the app's
        # internal API (POST .../finish) to complete the run — only
        # complete it here if that somehow didn't happen.
        with db.get_session() as session:
            dm = DatasetManager(session)
            tm = TrainingManager(session, dm)
            current_status = tm.get_run(run_id).status.value
            if current_status not in ("SUCCESS", "FAILED", "CANCELLED"):
                TrainingService(training_manager=tm, orchestrator=orchestrator, tracker=tracker).complete_run(run_id)
                current_status = tm.get_run(run_id).status.value
        tracker.end_run(status="SUCCESS")
        _detail(f"TrainingRun {run_id} status={current_status}")
    finally:
        close = getattr(orchestrator, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------------ #
    # 6. Model evaluation + promotion (decided by the DAG) + serving reload
    # ------------------------------------------------------------------ #
    # The DAG's `register_and_promote` task already applied
    # ModelPromotionPolicy server-side and, on approval, published the
    # ServingBridge reload — this step only reads back what it decided.
    step(6, 6, "Model evaluation + promotion (decided by the DAG) + serving reload")
    with db.get_session() as session:
        latest_mv = session.execute(
            select(ModelVersion)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number.desc())
            .limit(1)
        ).scalars().first()
        if latest_mv is None:
            raise SystemExit("no ModelVersion found after training")
        metrics = json.loads(latest_mv.metrics_json or "{}")
        for k, v in metrics.items():
            _detail(f"{k:20} {v:.4f}" if isinstance(v, float) else f"{k}: {v}")
        _detail(f"ModelVersion v{latest_mv.version_number} state={latest_mv.state.value}")
        promoted = latest_mv.state == ModelState.PRODUCTION
        if not promoted:
            _detail(f"! model not in PRODUCTION (state={latest_mv.state.value})")
        result = InitialTrainingResult(
            dataset_id=dataset_id,
            dataset_version_id=version_id,
            model_id=model_id,
            model_version_id=latest_mv.id,
            model_version_number=latest_mv.version_number,
            model_state=latest_mv.state.value,
            metrics=metrics,
            promoted=promoted,
            tracker_run_id=tracker_run_id,
            mlflow_run_id=latest_mv.mlflow_run_id,
            execution_id=execution_id,
            dag_id=dag_id,
            content_sha256=content_sha256,
            csv_path=csv_local_path,
        )

    if result.promoted:
        resp = httpx.get(f"{endpoints['serving_url']}/internal/model/active/{model_name}", timeout=5.0)
        if resp.status_code == 200:
            _detail(f"serving bridge reports active model_version={resp.json().get('model_version_number')}")
        else:
            _detail(f"! serving bridge returned {resp.status_code}: {resp.text}")
    else:
        _detail("skipped serving reload — model was not promoted")

    _detail(f"MLflow run id: {tracker_run_id} — search it under experiment {experiment_name!r} at {endpoints['mlflow_uri']}")
    _detail(f"Airflow run:   {endpoints['airflow_url']}/dags/{dag_id}/grid?dag_run_id={execution_id.split('/', 1)[-1]}")

    return result
