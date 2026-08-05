"""Drive the *deployed* stack through a full fraud-detection lifecycle.

Same story as ``run_fraud_detection_e2e.py``, but nothing runs locally
except this script. Every record lands in the deployed RDS instance and
shows up at the Management UI, and the training itself is executed by
the deployed Airflow scheduler.

Why a client script and not direct database access
--------------------------------------------------

RDS accepts connections from the app's security group and nowhere else,
so there is no route from a laptop to the database. Everything therefore
goes through ``/api/internal/*`` on the deployed app, which is the same
surface the Airflow DAG uses.

Flow::

    this script            deployed app              deployed Airflow
    -----------            ------------              ----------------
    POST /datasets      -> DatasetManager
    POST /versions      -> DatasetManager  (S3 URI)
    POST /readiness     -> ReadinessEngine
    POST /models        -> ModelManager
    POST /training-runs -> TrainingManager (PENDING)
    POST /start         -> MLflowTracker.start_run
                           AirflowOrchestrator  ----> triggers DAG
                                                      resolve_context
                                                      train (XGBoost)
                                                      register_and_promote
                        <- POST /finish     <-------- report_status
    poll GET /training-runs/{id}

The dataset profile (row count, schema, fraud ratio, content hash) is
computed here from the local CSV, because the app has no reason to hold
a 144 MB file. The URI registered is the S3 one, which is what the
Airflow worker actually reads.

Usage::

    APP_BASE_URL=http://<host>:8000 \\
    python -m scripts.run_fraud_detection_deployed \\
        --csv case_studies/fraud_detection/data/creditcard.csv \\
        --s3-uri s3://<bucket>/datasets/creditcard.csv
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mlops_framework.dataset.checksum import calculate_file_checksum  # noqa: E402

from case_studies.fraud_detection import data as fraud_data  # noqa: E402

DATASET_NAME = "credit-card-fraud"
MODEL_NAME = "fraud-xgboost"
DAG_ID = "mlops_training_pipeline"
TRAINING_ENTRYPOINT = "case_studies.fraud_detection.pipelines:train_xgboost"


def _say(step: str) -> None:
    print(f"\n=== {step} ===", flush=True)


def _detail(message: str) -> None:
    print(f"  • {message}", flush=True)


class AppClient:
    """Thin wrapper over the deployed app's internal API."""

    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self._c = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> "AppClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def post(self, path: str, body: dict) -> dict:
        r = self._c.post(f"/api/internal{path}", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code} {r.text}")
        return r.json()

    def get_run(self, run_id: int) -> dict:
        r = self._c.get(f"/api/training-runs/{run_id}")
        r.raise_for_status()
        return r.json()

    def get(self, path: str) -> dict:
        r = self._c.get(f"/api{path}")
        r.raise_for_status()
        return r.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-base-url", default=os.environ.get("APP_BASE_URL", "http://localhost:8000")
    )
    parser.add_argument(
        "--csv",
        default=str(
            REPO_ROOT / "case_studies" / "fraud_detection" / "data" / "creditcard.csv"
        ),
    )
    parser.add_argument(
        "--s3-uri",
        required=True,
        help="s3:// URI the Airflow worker will read the CSV from",
    )
    parser.add_argument("--timeout", type=float, default=2400.0)
    parser.add_argument("--poll-interval", type=float, default=15.0)
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"error: {csv_path} not found", file=sys.stderr)
        return 2

    with AppClient(args.app_base_url) as app:
        _say("1/6  Dataset registration")
        dataset = app.post(
            "/datasets",
            {
                "name": DATASET_NAME,
                "description": (
                    "Kaggle Credit Card Fraud Detection — 284,807 European "
                    "card transactions over 48 hours, 492 fraudulent."
                ),
            },
        )
        _detail(f"dataset id={dataset['id']} (created={dataset['created']})")

        _detail("profiling the local CSV …")
        profile = fraud_data.describe_csv(csv_path)
        metadata = dict(profile["metadata"])
        metadata["content_sha256"] = calculate_file_checksum(csv_path)
        metadata["size_bytes"] = csv_path.stat().st_size

        version = app.post(
            f"/datasets/{dataset['id']}/versions",
            {
                "storage_uri": args.s3_uri,
                "row_count": profile["row_count"],
                "metadata": metadata,
            },
        )
        _detail(
            f"version v{version['version_number']} id={version['id']} "
            f"(created={version['created']}) — {version['row_count']:,} rows"
        )
        _detail(f"storage_uri = {version['storage_uri']}")
        _detail(f"schema_hash = {version['schema_hash'][:16]}…")

        _say("2/6  Readiness evaluation")
        readiness = app.post(
            f"/readiness/{version['id']}",
            {
                "policy": {
                    "required_size": 100_000,
                    "required_columns": fraud_data.feature_columns()
                    + [fraud_data.target_column()],
                    "expected_column_count": 31,
                    "max_missing_ratio": 0.0,
                }
            },
        )
        for name, outcome in readiness["checks"].items():
            _detail(f"{name:20} {outcome}")
        _detail(f"=> {readiness['status']}")
        if not readiness["is_ready"]:
            for reason in readiness["reasons"]:
                _detail(f"blocked: {reason}")
            print("\nDataset is BLOCKED — the framework refuses to train on it.")
            return 1

        _say("3/6  Model registration")
        model = app.post(
            "/models",
            {
                "name": MODEL_NAME,
                "task": "binary_classification",
                "description": "XGBoost fraud classifier on Kaggle credit-card data",
            },
        )
        _detail(f"model id={model['id']} (created={model['created']})")

        _say("4/6  Training run — executed by the deployed Airflow")
        run = app.post(
            "/training-runs",
            {
                "dataset_version_id": version["id"],
                "pipeline_id": DAG_ID,
                "trigger_type": "API",
                "metadata": {
                    # The DAG reads these out of /context. `pipeline_id` above
                    # is the dag_id AirflowOrchestrator triggers; the Python
                    # callable that does the training is a different thing and
                    # travels under its own name.
                    "training_entrypoint": TRAINING_ENTRYPOINT,
                    "model_name": MODEL_NAME,
                    "min_f1": 0.70,
                    "orchestrator": "AirflowOrchestrator",
                    "parameters": {
                        "n_estimators": 200,
                        "max_depth": 6,
                        "learning_rate": 0.1,
                    },
                },
            },
        )
        run_id = run["id"]
        _detail(f"TrainingRun {run_id} created (PENDING)")

        # ECS Service Connect's ingress listener cuts a request at 60s, so a
        # slow /start is reported to the client as a dropped connection even
        # when the server carries on. Treat that as "probably started" and
        # let the poll below establish the truth, rather than failing a run
        # that is already on its way.
        try:
            started = app.post(f"/training-runs/{run_id}/start", {"dag_id": DAG_ID})
        except (httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            _detail(f"start request dropped ({type(exc).__name__}); checking the run")
            started = app.get_run(run_id)
        _detail(f"MLflow run  = {started.get('mlflow_run_id')}")
        _detail(f"Airflow run = {started.get('execution_id')}")
        _detail(f"status      = {started['status']}")

        _say("5/6  Waiting for Airflow to finish the training")
        deadline = time.time() + args.timeout
        last = None
        while time.time() < deadline:
            current = app.get_run(run_id)
            if current["status"] != last:
                _detail(f"status: {current['status']}")
                last = current["status"]
            if current["status"] in {"SUCCESS", "FAILED", "CANCELLED"}:
                break
            time.sleep(args.poll_interval)
        else:
            print(f"\nTimed out after {args.timeout}s waiting for run {run_id}.")
            return 1

        final = app.get_run(run_id)
        if final["status"] != "SUCCESS":
            _detail(f"error: {final.get('error_message')}")
            print(f"\nTraining did not succeed ({final['status']}).")
            return 1
        for key, value in (final.get("metrics") or {}).items():
            _detail(
                f"{key:20} {value:.4f}" if isinstance(value, float) else f"{key}: {value}"
            )
        if final.get("duration_seconds"):
            _detail(f"duration: {final['duration_seconds']:.0f}s")

        _say("6/6  Model registry")
        versions = app.get(f"/models/{model['id']}/versions")
        for mv in versions:
            metrics = mv.get("metrics") or {}
            summary = ", ".join(
                f"{k}={v:.3f}" for k, v in metrics.items() if isinstance(v, float)
            )
            _detail(f"v{mv['version_number']:<3} {mv['state']:11} {summary}")

    print(f"\nDone. Open the UI: {args.app_base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
