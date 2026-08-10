"""End-to-end MLOps demo against real MLflow + Airflow + serving bridge.

The demo wires up the framework with the production-side adapters
(``MLflowTracker`` and ``AirflowOrchestrator``) and runs a full
governance chain against the running Docker Compose stack:

    Dataset
        -> DatasetVersion
            -> ReadinessEngine (READINESS)
                -> TrainingEligibilityPolicy (ELIGIBLE, force=True on first pass)
                    -> AirflowOrchestrator DAG run
                        -> train_xgboost (real XGBoost, MLflow logs)
                    -> TrainingRun SUCCESS
                    -> ModelVersion (CANDIDATE)
                        -> ModelPromotionPolicy (APPROVED)
                    -> ModelVersion (PRODUCTION)
                        -> HttpEventPublisher -> ServingBridge reload

It assumes the stack is already up (i.e. ``docker compose --env-file
.env.docker up -d`` has been run). The script prints the URL to the
MLflow, Airflow, and Management UI dashboards at the end.

The script can also be invoked directly on the host (without Docker)
provided the host can reach the MLflow, Airflow, and ServingBridge
services — the URLs are picked up from environment variables
(``MLFLOW_TRACKING_URI``, ``AIRFLOW_BASE_URL``, ``SERVING_BRIDGE_URL``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select

# Make the package and the case studies importable.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mlops_framework.config.settings import get_settings
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.database.session import DatabaseManager
from mlops_framework.governance.eligibility import (
    EligibilityConfig,
    EligibilityDecision,
    TrainingEligibilityPolicy,
)
from mlops_framework.lineage.manager import LineageManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.airflow import AirflowOrchestrator
from mlops_framework.readiness.engine import ReadinessEngine, TrainingPolicy
from mlops_framework.sdk import MLOpsProject
from mlops_framework.tracking.mlflow import MLflowTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService

from case_studies.fraud_detection import data as fraud_data
from case_studies.fraud_detection.pipelines import train_xgboost  # noqa: F401


DATASET_NAME = "credit-card-transactions"
MODEL_NAME = "fraud-xgboost"
PIPELINE_FRIENDLY = "fraud-xgboost-real"
PIPELINE_ID = "case_studies.fraud_detection.pipelines:train_xgboost"
# AirflowOrchestrator.trigger_pipeline() takes the DAG id, not the
# module:callable path — see mlops_training_pipeline.py's
# _resolve_entrypoint(). The callable travels separately in
# metadata["training_entrypoint"].
DAG_ID = os.environ.get("AIRFLOW_DAG_ID", "mlops_training_pipeline")
DATA_ROWS = 5000

STEP_SEP = "─" * 72


def _print_banner(title: str) -> None:
    print(f"\n{STEP_SEP}\n  {title}\n{STEP_SEP}")


def _wait_for(url: str, *, label: str, timeout: float = 180.0) -> None:
    """Poll ``url`` until it returns 2xx/3xx or we time out."""
    print(f"  waiting for {label} at {url} ...", flush=True)
    deadline = time.time() + timeout
    last_err: str | None = None
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


def _resolve_endpoints(settings) -> dict[str, str]:
    """Resolve the service URLs the demo will talk to.

    Honors explicit env vars first (Docker), falls back to the
    ``Settings`` instance (which itself reads the same env vars for
    out-of-container runs).
    """
    mlflow_uri = (
        os.environ.get("MLFLOW_TRACKING_URI")
        or settings.mlflow_tracking_uri
        or "http://localhost:5000"
    )
    airflow_url = (
        os.environ.get("AIRFLOW_BASE_URL")
        or settings.airflow_base_url
        or "http://localhost:8080"
    )
    serving_url = (
        os.environ.get("SERVING_BRIDGE_URL")
        or settings.serving_bridge_url
        or "http://localhost:8001"
    )
    return {
        "mlflow_uri": mlflow_uri,
        "airflow_url": airflow_url,
        "serving_url": serving_url,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the end-to-end MLOps demo.")
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="Skip the initial readiness check (useful for offline debugging)",
    )
    args = parser.parse_args()

    # Refresh settings — the .env.docker file or container env vars
    # win over the on-host .env.
    get_settings.cache_clear()
    settings = get_settings()
    endpoints = _resolve_endpoints(settings)

    print("MLOps Framework — Real End-to-End Demo")
    print("─" * 72)
    print(f"  DATABASE_URL     = {settings.database_url}")
    print(f"  MLFLOW_TRACKING  = {endpoints['mlflow_uri']}")
    print(f"  AIRFLOW_BASE_URL = {endpoints['airflow_url']}")
    print(f"  SERVING_BRIDGE   = {endpoints['serving_url']}")

    if not args.skip_wait:
        _wait_for(f"{endpoints['airflow_url']}/health", label="Airflow")
        _wait_for(f"{endpoints['mlflow_uri']}/health", label="MLflow")
        _wait_for(f"{endpoints['serving_url']}/healthz", label="ServingBridge")

    # ------------------------------------------------------------------ #
    # Wire the framework with production adapters
    # ------------------------------------------------------------------ #
    _print_banner("Wiring framework adapters (MLflow + Airflow)")

    db = DatabaseManager()
    tracker = MLflowTracker(
        tracking_uri=endpoints["mlflow_uri"],
        experiment_name="fraud-demo",
    )
    orchestrator = AirflowOrchestrator(
        base_url=endpoints["airflow_url"],
        username=settings.airflow_username or "airflow",
        password=settings.airflow_password or "airflow",
    )

    project = MLOpsProject(
        "fraud-detection-demo",
        orchestrator=orchestrator,
        tracker=tracker,
        db_manager=db,
    )

    # Register the XGBoost pipeline as a friendly name so application
    # code doesn't have to know the module path.
    project.register_pipeline(
        name=PIPELINE_FRIENDLY,
        pipeline_id=PIPELINE_ID,
        description="Real XGBoost trainer for the Fraud Detection case study",
    )

    # ------------------------------------------------------------------ #
    # 1. Dataset + version
    # ------------------------------------------------------------------ #
    _print_banner("1. Dataset + DatasetVersion (Fraud Detection, synthetic CSV)")

    data_path = ROOT / "case_studies" / "fraud_detection" / "data" / "transactions.csv"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    if not data_path.exists():
        fraud_data.write_csv(data_path, n_rows=DATA_ROWS)
        print(f"  • wrote synthetic fraud dataset → {data_path}")
    else:
        print(f"  • reusing existing dataset at {data_path}")

    # The DatasetVersion's storage_uri is what the *Airflow* `train` task
    # reads (via resolve_context's payload["storage_uri"]), not this
    # process. The airflow image is a separate build
    # (infrastructure/airflow/Dockerfile) that bakes case_studies/ in at
    # /opt/case_studies — a different root than this image's /opt/framework
    # (infrastructure/app/Dockerfile) — so the path recorded here must be
    # valid from the Airflow container's filesystem, not this one's.
    airflow_csv_path = "/opt/case_studies/fraud_detection/data/transactions.csv"

    try:
        ds = project.get_dataset(DATASET_NAME)
    except Exception:
        ds = project.create_dataset(DATASET_NAME, description="Synthetic credit-card fraud")

    if not any(v.storage_uri == airflow_csv_path for v in ds.versions):
        version = ds.create_version(
            storage_uri=airflow_csv_path,
            row_count=DATA_ROWS,
            metadata=fraud_data.schema_metadata(),
        )
    else:
        version = ds.latest_version
    print(f"  • dataset_id={ds.id} version_id={version.id} rows={version.row_count}")

    # ------------------------------------------------------------------ #
    # 2. Readiness
    # ------------------------------------------------------------------ #
    _print_banner("2. ReadinessEngine → READY?")

    policy = TrainingPolicy(
        required_size=1000,
        freshness_hours=24 * 365,
        required_columns=["time", "amount", "v1", "v28", "class"],
        dtypes={
            "time": "float64",
            "amount": "float64",
            "class": "int64",
        },
        expected_column_count=31,
    )

    with db.get_session() as session:
        version_row = session.get(DatasetVersion, version.id)
        readiness = ReadinessEngine(session).evaluate(version_row, policy)
        print(f"  • status={readiness.status.value} checks={readiness.check_dict()}")
        if not readiness.is_ready:
            print(f"  ✗ dataset BLOCKED: {readiness.reasons}")
            return 1

    # ------------------------------------------------------------------ #
    # 3. Eligibility (force=True for the first demo pass)
    # ------------------------------------------------------------------ #
    _print_banner("3. TrainingEligibilityPolicy")

    with db.get_session() as session:
        model_row = (
            session.query(ModelRow).filter_by(name=MODEL_NAME).first()
        )
        if model_row is None:
            model_row = ModelManager(session).create_model(
                name=MODEL_NAME,
                task="binary_classification",
                description="Fraud vs legit credit-card transactions",
            )
            session.commit()
        model_id = model_row.id

    # Build a context directly so we control the eligibility config
    # (force=True on the first demo pass to bypass cooldown / new-row
    # gates).
    with db.get_session() as session:
        eligibility_policy = TrainingEligibilityPolicy(session)
        version_row = session.get(DatasetVersion, version.id)
        model_row = session.get(ModelRow, model_id)
        eligibility_ctx = eligibility_policy.build_context(
            dataset_version=version_row,
            readiness=readiness,
            model=model_row,
            force=True,
        )
        eligibility_decision: EligibilityDecision = eligibility_policy.evaluate(
            eligibility_ctx, EligibilityConfig()
        )
        print(
            f"  • eligible={eligibility_decision.eligible} "
            f"reasons={eligibility_decision.reasons}"
        )

    # ------------------------------------------------------------------ #
    # 4. + 5. Training + orchestration (real XGBoost inside Airflow)
    # ------------------------------------------------------------------ #
    _print_banner("4. Training (Airflow DAG → train_xgboost)")

    # We drive the TrainingService directly so the demo can wire the
    # MLflowTracker + AirflowOrchestrator in one place and report
    # progress step-by-step. The TrainingService is what
    # ``project.train(...)`` uses internally.
    with db.get_session() as session:
        dm = DatasetManager(session)
        tm = TrainingManager(session, dm)
        service = TrainingService(
            training_manager=tm, orchestrator=orchestrator, tracker=tracker
        )

        run = service.create_run(
            dataset_version_id=version.id,
            pipeline_id=DAG_ID,
            trigger_type="MANUAL",
            metadata={
                "training_entrypoint": PIPELINE_ID,
                "parameters": {
                    "max_depth": 6,
                    "n_estimators": 150,
                    "learning_rate": 0.1,
                },
                "csv_uri": airflow_csv_path,
                "model_name": MODEL_NAME,
                "min_f1": 0.5,
                "tracking_uri": endpoints["mlflow_uri"],
            },
        )
        run_id = run.id
        print(f"  • TrainingRun created (id={run_id})")

        # Start the MLflowTracker run *before* the orchestrator trigger
        # so the Airflow task can reuse the same run id.
        tracker_run_id = tracker.start_run(
            run_name=f"training-run-{run_id}",
            tags={"training_run_id": str(run_id), "pipeline_id": PIPELINE_ID},
        )
        tracker.log_params(
            {"pipeline": PIPELINE_FRIENDLY, "csv_uri": str(data_path)}
        )
        print(f"  • MLflow run started (id={tracker_run_id})")
        # Persist the tracker run id on the TrainingRun metadata so
        # the Airflow task can pick it up.
        tm.update_metadata(
            run_id,
            {
                "tracker_run_id": tracker_run_id,
                "tracking_uri": endpoints["mlflow_uri"],
                "orchestrator_execution_id": "pending",
            },
        )
        execution_id = service.start_run(run_id)
        tm.update_metadata(
            run_id, {"orchestrator_execution_id": execution_id}
        )
        print(f"  • triggered Airflow DAG run id={execution_id}")

    # ------------------------------------------------------------------ #
    # 6. Wait for the Airflow DAG run to reach SUCCESS / FAILED
    # ------------------------------------------------------------------ #
    _print_banner("5. Waiting for the Airflow DAG run to finish")

    deadline = time.time() + 600.0  # XGBoost on 5k rows is sub-second but
    final_state = "UNKNOWN"
    while time.time() < deadline:
        status = orchestrator.get_execution_status(execution_id)
        print(
            f"  • {status.state.value:<10} "
            f"(task_states="
            f"{orchestrator.get_task_instances(execution_id)})"
        )
        if status.state.value in ("SUCCESS", "FAILED", "CANCELLED"):
            final_state = status.state.value
            break
        time.sleep(3.0)

    if final_state != "SUCCESS":
        tracker.end_run(status="FAILED")
        print(f"\n  ✗ Airflow DAG run did not succeed (final={final_state})")
        return 2

    # Sync the TrainingRun lifecycle. The DAG's own `report_status` task
    # already calls the app's internal API (POST .../finish) to complete
    # the run — see mlops_training_pipeline.py — so by the time the DAG
    # itself reaches SUCCESS the TrainingRun is normally already
    # terminal. Only call complete_run here if that somehow didn't happen.
    with db.get_session() as session:
        dm = DatasetManager(session)
        tm = TrainingManager(session, dm)
        current_status = tm.get_run(run_id).status.value
        if current_status not in ("SUCCESS", "FAILED", "CANCELLED"):
            service = TrainingService(
                training_manager=tm, orchestrator=orchestrator, tracker=tracker
            )
            service.complete_run(run_id)
            current_status = tm.get_run(run_id).status.value
        tracker.end_run(status="SUCCESS")
        print(f"  ✓ TrainingRun {run_id} status={current_status}")

    # ------------------------------------------------------------------ #
    # 7. Model evaluation + promotion
    # ------------------------------------------------------------------ #
    # The DAG's own `register_and_promote` task already applied the
    # ModelPromotionPolicy server-side (POST .../models/{name}/promote,
    # see mlops_framework.api.routers.internal.promote_model) and, on
    # approval, published the ModelPromotedEvent to the ServingBridge
    # itself (see register_and_promote() in mlops_training_pipeline.py).
    # Re-running the policy and re-transitioning state here would just
    # double-apply a decision already made — and crash, since the
    # candidate is no longer in a state APPROVED/PRODUCTION can be
    # entered from a second time. So this step only reads back what the
    # DAG decided.
    _print_banner("6. Model evaluation + PromotionPolicy (decided by the DAG)")

    with db.get_session() as session:
        latest_mv = session.execute(
            select(ModelVersion)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number.desc())
            .limit(1)
        ).scalars().first()
        if latest_mv is None:
            print("  ✗ no ModelVersion found after training")
            return 3

        print(f"  • ModelVersion v{latest_mv.version_number} state={latest_mv.state.value}")
        print(f"  • candidate metrics={json.loads(latest_mv.metrics_json or '{}')}")

        promoted_id: int | None = None
        promoted_version: int | None = None
        if latest_mv.state == ModelState.PRODUCTION:
            promoted_id = latest_mv.id
            promoted_version = latest_mv.version_number
        else:
            print(f"  ✗ model not in PRODUCTION (state={latest_mv.state.value})")

    # ------------------------------------------------------------------ #
    # 8. Confirm the ServingBridge reload the DAG already triggered
    # ------------------------------------------------------------------ #
    if promoted_id is not None:
        _print_banner("7. ServingBridge — confirm the DAG's reload took effect")
        resp = httpx.get(
            f"{endpoints['serving_url']}/internal/model/active/{MODEL_NAME}",
            timeout=5.0,
        )
        if resp.status_code == 200:
            body = resp.json()
            print(
                f"  • serving bridge reports active model_version="
                f"{body.get('model_version_number')}"
            )
        else:
            print(f"  ! serving bridge returned {resp.status_code}: {resp.text}")
    else:
        print("\n7. Skipped serving reload — model was not promoted")

    # ------------------------------------------------------------------ #
    # 9. Lineage
    # ------------------------------------------------------------------ #
    _print_banner("8. Lineage (model-version chain)")

    with db.get_session() as session:
        lm = LineageManager(session)
        graph = lm.graph_for_model_version(promoted_id or latest_mv.id)
        print(
            f"  • nodes={len(graph.nodes)} edges={len(graph.edges)} "
            f"root={graph.root_kind}:{graph.root_id}"
        )
        for node in graph.nodes:
            print(f"     - {node.type}: {node.label}")
        for edge in graph.edges:
            print(f"     - {edge.source} → {edge.target} ({edge.type})")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    _print_banner("✓ Demo complete")
    print("Inspect the results:")
    print(f"  • Management UI   {settings.app_name} API  : http://localhost:8000")
    print(f"  • MLflow UI       (runs, params, metrics)     : {endpoints['mlflow_uri']}")
    print(f"  • Airflow UI      (DAG runs, task logs)       : {endpoints['airflow_url']}")
    print(f"  • MinIO console   (artifacts)                 : http://localhost:9001")
    print(
        f"  • ServingBridge   (active model version)      : "
        f"{endpoints['serving_url']}/internal/model/active/{MODEL_NAME}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())