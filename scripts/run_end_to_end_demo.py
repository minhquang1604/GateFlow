"""End-to-end MLOps demo against real MLflow + Airflow + serving bridge.

The demo trains a fresh model through the full governance chain against
the running Docker Compose stack:

    Dataset
        -> DatasetVersion (content-hash pinned)
            -> ReadinessEngine (READY)
                -> TrainingEligibilityPolicy (ELIGIBLE, force=True on
                   the first pass)
                    -> AirflowOrchestrator DAG run
                        -> train_xgboost (real XGBoost, MLflow logs)
                    -> TrainingRun SUCCESS
                    -> ModelVersion (CANDIDATE)
                        -> ModelPromotionPolicy (APPROVED, decided
                           server-side by the DAG's own task)
                    -> ModelVersion (PRODUCTION)
                        -> HttpEventPublisher -> ServingBridge reload
                    -> Lineage graph

That flow itself lives in :mod:`scripts._initial_training` — shared
with ``run_drift_recovery_demo.py``'s Phase 1, which trains the same
way before injecting drift. This script just supplies the fraud-demo's
dataset/model names and prints the lineage graph + a final summary
afterwards.

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
import os
import sys
from pathlib import Path

# Make the package and the case studies importable.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager
from mlops_framework.lineage.manager import LineageManager
from scripts._initial_training import (
    STEP_SEP,
    _print_banner,
    _wait_for,
    resolve_endpoints,
    run_initial_training,
)

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

# The DatasetVersion's storage_uri is what the *Airflow* `train` task
# reads (via resolve_context's payload["storage_uri"]), not this
# process. The airflow image is a separate build
# (infrastructure/airflow/Dockerfile) that bakes case_studies/ in at
# /opt/case_studies — a different root than this image's /opt/framework
# (infrastructure/app/Dockerfile) — so the path recorded here must be
# valid from the Airflow container's filesystem, not this one's.
AIRFLOW_CSV_PATH = "/opt/case_studies/fraud_detection/data/transactions.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the end-to-end MLOps demo.")
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="Skip the initial readiness check (useful for offline debugging)",
    )
    parser.add_argument(
        "--timeout", type=float, default=600.0, help="Airflow DAG run timeout, seconds"
    )
    args = parser.parse_args()

    # Refresh settings — the .env.docker file or container env vars
    # win over the on-host .env.
    get_settings.cache_clear()
    settings = get_settings()
    endpoints = resolve_endpoints(settings)

    print("MLOps Framework — Real End-to-End Demo")
    print(STEP_SEP)
    print(f"  DATABASE_URL     = {settings.database_url}")
    print(f"  MLFLOW_TRACKING  = {endpoints['mlflow_uri']}")
    print(f"  AIRFLOW_BASE_URL = {endpoints['airflow_url']}")
    print(f"  SERVING_BRIDGE   = {endpoints['serving_url']}")

    if not args.skip_wait:
        _wait_for(f"{endpoints['airflow_url']}/health", label="Airflow")
        _wait_for(f"{endpoints['mlflow_uri']}/health", label="MLflow")
        _wait_for(f"{endpoints['serving_url']}/healthz", label="ServingBridge")

    db = DatabaseManager(settings.database_url)
    # Real Postgres deployments are already migrated (alembic); this is a
    # no-op there but lets the demo also run cold against a fresh SQLite
    # file (e.g. under a hermetic test or a quick local trial).
    Base.metadata.create_all(db.engine)

    result = run_initial_training(
        db,
        endpoints,
        settings,
        dataset_name=DATASET_NAME,
        dataset_description="Synthetic credit-card fraud",
        model_name=MODEL_NAME,
        model_description="Fraud vs legit credit-card transactions",
        pipeline_friendly=PIPELINE_FRIENDLY,
        pipeline_id=PIPELINE_ID,
        dag_id=DAG_ID,
        experiment_name="fraud-demo",
        csv_local_path=ROOT / "case_studies" / "fraud_detection" / "data" / "transactions.csv",
        airflow_csv_path=AIRFLOW_CSV_PATH,
        csv_write_kwargs={"n_rows": DATA_ROWS},
        training_params={"max_depth": 6, "n_estimators": 150, "learning_rate": 0.1},
        min_f1=0.5,
        timeout=args.timeout,
    )

    if not result.promoted:
        print(f"\n  ✗ model not in PRODUCTION (state={result.model_state})")
        return 3

    # ------------------------------------------------------------------ #
    # Lineage
    # ------------------------------------------------------------------ #
    _print_banner("Lineage (model-version chain)")
    with db.get_session() as session:
        lm = LineageManager(session)
        graph = lm.graph_for_model_version(result.model_version_id)
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
    print("  • MinIO console   (artifacts)                 : http://localhost:9001")
    print(
        f"  • ServingBridge   (active model version)      : "
        f"{endpoints['serving_url']}/internal/model/active/{MODEL_NAME}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
