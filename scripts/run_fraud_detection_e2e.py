"""End-to-end Fraud Detection run driven entirely by the framework.

Trains XGBoost on the real Kaggle credit-card-fraud dataset (284,807
transactions, 492 fraudulent) and records every step through the
framework's own components, so the result is inspectable in the
Management UI afterwards.

What is real here
-----------------

* **Dataset** — the real ``creditcard.csv``. Its checksum is computed
  over the file's bytes, its schema hash over the observed dtypes, and
  its row count is read, not declared.
* **Training** — real ``xgboost.XGBClassifier`` on all 284,807 rows,
  executed by ``LocalDockerOrchestrator`` as an actual subprocess.
* **Experiment tracking** — a real MLflow tracking server. Params,
  metrics and the serialized model land in its backing store.
* **Orchestration** — a real Airflow deployment. The framework triggers
  a DAG run through ``AirflowOrchestrator``, polls it, and cancels it,
  recording the lifecycle against a second TrainingRun.

What is *not* claimed
---------------------

Airflow does not execute the XGBoost training in this script. Its
scheduler runs inside the deployed VPC and writes to the deployed
database; it cannot reach the local SQLite database this script builds,
and the DAG's tasks call the deployed app's internal API rather than
this process. So the training runs locally and Airflow is exercised as
the orchestrator it is — triggered, polled, cancelled — with its
execution id recorded on the run. Read the run detail page: the Airflow
run is labelled as such.

Usage::

    MLFLOW_TRACKING_URI=http://<host>:5000 \\
    AIRFLOW_BASE_URL=http://<host>:8080 \\
    AIRFLOW_USERNAME=admin AIRFLOW_PASSWORD=... \\
    python -m scripts.run_fraud_detection_e2e

Then serve the UI against the database it wrote::

    DATABASE_URL=sqlite:///fraud_demo.db \\
    uvicorn mlops_framework.api.app:create_app --factory --port 8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mlops_framework.database.base import Base  # noqa: E402
from mlops_framework.database.models.model_version import ModelState  # noqa: E402
from mlops_framework.database.session import DatabaseManager  # noqa: E402
from mlops_framework.dataset.checksum import calculate_file_checksum  # noqa: E402
from mlops_framework.dataset.manager import DatasetManager  # noqa: E402
from mlops_framework.governance.promotion import (  # noqa: E402
    ModelPromotionPolicy,
    PromotionConfig,
    PromotionContext,
)
from mlops_framework.lineage.manager import LineageManager  # noqa: E402
from mlops_framework.model.manager import ModelManager  # noqa: E402
from mlops_framework.orchestration.local import LocalDockerOrchestrator  # noqa: E402
from mlops_framework.readiness.engine import ReadinessEngine, TrainingPolicy  # noqa: E402
from mlops_framework.training.manager import TrainingManager  # noqa: E402
from mlops_framework.training.service import TrainingService  # noqa: E402

from case_studies.fraud_detection import data as fraud_data  # noqa: E402

DATASET_NAME = "credit-card-fraud"
MODEL_NAME = "fraud-xgboost"
PIPELINE_ID = "case_studies.fraud_detection.pipelines:train_xgboost"
DEFAULT_CSV = REPO_ROOT / "case_studies" / "fraud_detection" / "data" / "creditcard.csv"


def _say(step: str, message: str = "") -> None:
    print(f"\n=== {step} ===" + (f"\n{message}" if message else ""), flush=True)


def _detail(message: str) -> None:
    print(f"  • {message}", flush=True)


# ---------------------------------------------------------------------- #
# Steps
# ---------------------------------------------------------------------- #


def register_dataset(session, csv_path: Path) -> Any:
    """Create the dataset and a version describing the real CSV."""
    dm = DatasetManager(session)

    dataset = dm.get_dataset_by_name(DATASET_NAME)
    if dataset is None:
        dataset = dm.create_dataset(
            DATASET_NAME,
            description=(
                "Kaggle Credit Card Fraud Detection — 284,807 European "
                "card transactions over 48 hours, 492 fraudulent."
            ),
        )
        _detail(f"created dataset {dataset.name!r} (id={dataset.id})")
    else:
        _detail(f"dataset {dataset.name!r} already exists (id={dataset.id})")

    profile = fraud_data.describe_csv(csv_path)
    metadata = dict(profile["metadata"])

    # The framework's own version checksum hashes storage_uri + metadata,
    # not the bytes. Hash the file here too and carry it in metadata so the
    # version is pinned to actual content, which is what reproducibility
    # requires. (See the note in DatasetManager._calculate_version_checksum.)
    _detail("hashing the CSV (144 MB) ...")
    metadata["content_sha256"] = calculate_file_checksum(csv_path)
    metadata["size_bytes"] = csv_path.stat().st_size

    existing = [
        v
        for v in dm.list_versions(dataset.id)
        if json.loads(v.metadata_json or "{}").get("content_sha256")
        == metadata["content_sha256"]
    ]
    if existing:
        version = existing[-1]
        _detail(f"identical content already registered as v{version.version_number}")
        return version

    version = dm.create_version(
        dataset_id=dataset.id,
        storage_uri=str(csv_path),
        row_count=profile["row_count"],
        metadata=metadata,
    )
    _detail(
        f"registered v{version.version_number}: {version.row_count:,} rows, "
        f"{metadata['n_fraud']} fraud ({metadata['fraud_ratio']:.4%})"
    )
    _detail(f"schema_hash={version.schema_hash[:16]}…")
    _detail(f"content_sha256={metadata['content_sha256'][:16]}…")
    return version


def evaluate_readiness(session, version) -> Any:
    """Run the readiness engine and persist an auditable decision."""
    policy = TrainingPolicy(
        required_size=100_000,
        required_columns=fraud_data.feature_columns() + [fraud_data.target_column()],
        expected_column_count=31,
        max_missing_ratio=0.0,
    )
    result = ReadinessEngine(session).evaluate(version, policy=policy)

    for name, outcome in result.check_dict().items():
        _detail(f"{name:20} {outcome}")
    _detail(f"=> {result.status.value}")
    if not result.is_ready:
        for reason in result.reasons:
            _detail(f"blocked: {reason}")
    return result


def train(session, version, *, tracking_uri: Optional[str], timeout: float) -> Any:
    """Run the real XGBoost pipeline through the framework."""
    dm = DatasetManager(session)
    tm = TrainingManager(session, dm)

    tracker = None
    if tracking_uri:
        from mlops_framework.tracking.mlflow import MLflowTracker

        tracker = MLflowTracker(
            tracking_uri=tracking_uri, experiment_name="fraud-detection"
        )
        _detail(f"MLflow tracking at {tracking_uri} (experiment=fraud-detection)")
    else:
        _detail("no MLFLOW_TRACKING_URI — running without experiment tracking")

    orchestrator = LocalDockerOrchestrator()
    service = TrainingService(
        training_manager=tm, orchestrator=orchestrator, tracker=tracker
    )

    run = service.create_run(
        dataset_version_id=version.id,
        pipeline_id=PIPELINE_ID,
        trigger_type="MANUAL",
        metadata={
            "parameters": {"n_estimators": 200, "max_depth": 6, "learning_rate": 0.1},
            "model_name": MODEL_NAME,
            "orchestrator": "LocalDockerOrchestrator",
        },
    )
    session.commit()
    _detail(f"TrainingRun {run.id} created (PENDING)")

    try:
        execution_id = service.start_run(run.id)
        session.commit()
        _detail(f"started; execution_id={execution_id}")
        _detail(f"training XGBoost on {version.row_count:,} rows — this takes a while…")

        started = time.time()
        state = service.wait_for_completion(run.id, timeout=timeout, poll_interval=2.0)
        session.commit()
        _detail(f"orchestrator finished in {time.time() - started:.0f}s: {state}")
    finally:
        orchestrator.shutdown()

    run = tm.get_run(run.id)
    meta = tm.get_run_metadata(run.id)
    _detail(f"run status: {run.status.value}")
    if run.error_message:
        _detail(f"error: {run.error_message[:400]}")
    return run, meta


def register_and_promote(session, run, version, metrics: dict) -> Any:
    """Register the trained model and apply the promotion policy."""
    mm = ModelManager(session)

    model = mm.get_model_by_name(MODEL_NAME)
    if model is None:
        model = mm.create_model(
            MODEL_NAME,
            task="binary_classification",
            description="XGBoost fraud classifier on Kaggle credit-card data",
        )
        _detail(f"created model {MODEL_NAME!r} (id={model.id})")

    candidate = mm.create_model_version(
        model_id=model.id,
        dataset_version_id=version.id,
        training_run_id=run.id,
        mlflow_run_id=run.mlflow_run_id,
        state=ModelState.CANDIDATE,
        metrics=metrics,
        artifact_uri=metrics.pop("_artifact_uri", None),
    )
    _detail(f"ModelVersion v{candidate.version_number} registered as CANDIDATE")

    production = next(
        (v for v in mm.list_model_versions(model.id) if v.state == ModelState.PRODUCTION),
        None,
    )

    # Gate on average_precision, not roc_auc: at a 0.17% positive rate a
    # model that never predicts fraud still scores ~0.95 ROC-AUC.
    decision = ModelPromotionPolicy().evaluate(
        context=PromotionContext(candidate=candidate, production=production),
        config=PromotionConfig(
            min_metrics={"average_precision": 0.70, "recall": 0.70},
            must_beat_production=True,
            allow_cold_start=True,
        ),
    )
    for reason in decision.reasons:
        _detail(reason)

    if decision.approved:
        mm.transition_state(candidate.id, ModelState.APPROVED)
        # Archive the prior production version *before* promoting the new
        # one — see workflow/retraining.py's promotion step for why.
        if production is not None and production.id != candidate.id:
            mm.transition_state(production.id, ModelState.ARCHIVED)
            _detail(f"archived previous production v{production.version_number}")
        mm.transition_state(candidate.id, ModelState.PRODUCTION)
        _detail(f"=> APPROVED — v{candidate.version_number} is now PRODUCTION")
    else:
        mm.transition_state(candidate.id, ModelState.REJECTED)
        _detail(f"=> REJECTED — v{candidate.version_number}")

    session.commit()
    return candidate


def exercise_airflow(session, version) -> Optional[Any]:
    """Drive a real Airflow DAG run through the framework's orchestrator.

    This is the orchestration path, not the training path — see the module
    docstring. It proves the adapter's trigger/poll/cancel cycle against a
    live deployment and records the result as its own TrainingRun.
    """
    base_url = os.environ.get("AIRFLOW_BASE_URL")
    if not base_url:
        _detail("no AIRFLOW_BASE_URL — skipping the Airflow orchestration leg")
        return None

    from mlops_framework.orchestration.airflow import AirflowOrchestrator

    dag_id = os.environ.get("AIRFLOW_DAG_ID", "mlops_training_pipeline")
    dm = DatasetManager(session)
    tm = TrainingManager(session, dm)

    with AirflowOrchestrator(
        base_url=base_url,
        username=os.environ.get("AIRFLOW_USERNAME", "airflow"),
        password=os.environ.get("AIRFLOW_PASSWORD", "airflow"),
    ) as orchestrator:
        service = TrainingService(
            training_manager=tm, orchestrator=orchestrator, tracker=None
        )
        run = service.create_run(
            dataset_version_id=version.id,
            pipeline_id=dag_id,
            trigger_type="SCHEDULED",
            metadata={
                "orchestrator": "AirflowOrchestrator",
                "airflow_base_url": base_url,
                "note": (
                    "Orchestration-only leg: the deployed Airflow cannot "
                    "reach this local database, so the DAG is triggered, "
                    "polled and cancelled to exercise the adapter."
                ),
            },
        )
        session.commit()
        _detail(f"TrainingRun {run.id} created for DAG {dag_id!r}")

        execution_id = service.start_run(run.id)
        session.commit()
        _detail(f"triggered real DAG run: {execution_id}")

        status = orchestrator.get_execution_status(execution_id)
        _detail(f"polled status: {status.state.value}")
        tasks = orchestrator.get_task_instances(execution_id)
        _detail(f"task instances: {tasks or '{} (DAG is paused — none scheduled)'}")

        service.cancel_run(run.id)
        session.commit()
        _detail(f"cancelled; framework run status = {tm.get_run(run.id).status.value}")

        # Leave the deployment as we found it.
        orchestrator._client.delete(orchestrator._dag_run_url(execution_id))
        _detail("deleted the DAG run from Airflow (test cleanup)")
        return run


def show_lineage(session, model_version) -> None:
    graph = LineageManager(session).graph_for_model_version(model_version.id).to_dict()
    for node in graph["nodes"]:
        label = node.get("label") or node.get("name") or node["id"]
        _detail(f"{node['type']:16} {label}")
    _detail(f"{len(graph['edges'])} edges")


# ---------------------------------------------------------------------- #
# Entry point
# ---------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="path to creditcard.csv")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", f"sqlite:///{REPO_ROOT / 'fraud_demo.db'}"),
    )
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument(
        "--skip-airflow", action="store_true", help="skip the Airflow orchestration leg"
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(
            f"error: {csv_path} not found.\n"
            "Download the Kaggle Credit Card Fraud dataset — see "
            "case_studies/fraud_detection/README.md",
            file=sys.stderr,
        )
        return 2

    db = DatabaseManager(args.database_url)
    Base.metadata.create_all(db.engine)
    print(f"database: {args.database_url}")

    with db.get_session() as session:
        _say("1/6  Dataset registration")
        version = register_dataset(session, csv_path)
        session.commit()

        _say("2/6  Readiness evaluation")
        readiness = evaluate_readiness(session, version)
        session.commit()
        if not readiness.is_ready:
            print("\nDataset is BLOCKED — the framework refuses to train on it.")
            return 1

        _say("3/6  Training (real XGBoost via LocalDockerOrchestrator)")
        run, meta = train(
            session,
            version,
            tracking_uri=os.environ.get("MLFLOW_TRACKING_URI"),
            timeout=args.timeout,
        )
        if run.status.value != "SUCCESS":
            print(f"\nTraining did not succeed ({run.status.value}).")
            return 1

        result = meta.get("orchestrator_result") or {}
        metrics = dict(result.get("metrics") or {})
        if not metrics:
            print("\nPipeline returned no metrics — nothing to promote.")
            return 1
        metrics["_artifact_uri"] = result.get("artifact_path")
        for key, value in metrics.items():
            if not key.startswith("_"):
                _detail(f"{key:20} {value:.4f}" if isinstance(value, float) else f"{key}: {value}")

        _say("4/6  Model registration + promotion policy")
        model_version = register_and_promote(session, run, version, metrics)

        _say("5/6  Airflow orchestration (real deployment)")
        if args.skip_airflow:
            _detail("skipped by --skip-airflow")
        else:
            exercise_airflow(session, version)

        _say("6/6  Lineage")
        show_lineage(session, model_version)

    print(
        "\nDone. Serve the UI against this database:\n"
        f"  DATABASE_URL={args.database_url} \\\n"
        "  uvicorn mlops_framework.api.app:create_app --factory --port 8000"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
