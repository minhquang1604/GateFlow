"""End-to-end training pipeline used by the lifecycle test.

This pipeline is a minimal but real training flow:

    1. Read config {training_run_id, dataset_version_id, ...}
    2. Simulate training (deterministic — no randomness, no real data)
    3. Log a few params and metrics
    4. Write a model artifact
    5. Print a JSON report on stdout that the orchestrator captures

The pipeline demonstrates the intended call pattern:

    framework.tracking.ExperimentTracker (abstracted)
        -> adapter (MLflowTracker or InMemoryTracker)

Application code never imports mlflow directly.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def main(config: dict[str, Any]) -> dict[str, Any]:
    """End-to-end pipeline entry point.

    Uses the framework's ``ExperimentTracker`` abstraction only. No
    direct mlflow import. We import lazily so that mlflow is not a
    hard dependency.
    """
    from mlops_framework.tracking.in_memory import InMemoryTracker
    from mlops_framework.tracking.mlflow import MLflowTracker

    # In tests we use the in-memory tracker; in real runs MLflowTracker
    # would be used. We pick based on an env var so the same code path
    # works for both unit and integration scenarios.
    use_mlflow = os.environ.get("MLOPS_TRACKER") == "mlflow"
    tracker = MLflowTracker() if use_mlflow else InMemoryTracker()

    run_id = tracker.start_run(
        run_name=f"training-run-{config.get('training_run_id')}",
        tags={"pipeline_id": "fraud-training-pipeline"},
    )

    # Simulate training
    params = {
        "n_estimators": 100,
        "max_depth": 6,
        "learning_rate": 0.05,
    }
    tracker.log_params(params)

    # Determinstic metric: same input -> same output.
    seed = (config.get("training_run_id") or 0) + 1
    metrics = {
        "f1": round(0.80 + 0.001 * (seed % 50), 4),
        "roc_auc": round(0.85 + 0.001 * (seed % 30), 4),
        "loss": round(0.40 - 0.001 * (seed % 20), 4),
    }
    tracker.log_metrics(metrics, step=1)
    tracker.log_metrics(metrics, step=2)

    # Write a model artifact.
    tmpdir = tempfile.mkdtemp(prefix="mlops-artifact-")
    artifact_path = os.path.join(tmpdir, "model.txt")
    with open(artifact_path, "w") as f:
        json.dump({"params": params, "metrics": metrics}, f)
    tracker.log_artifact(artifact_path)

    tracker.end_run(status="SUCCESS")

    return {
        "status": "SUCCESS",
        "tracker_run_id": run_id,
        "artifact_path": artifact_path,
        "metrics": metrics,
    }
