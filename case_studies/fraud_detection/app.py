"""App entry point for the Fraud Detection case study.

This is the only file the case-study user writes. It uses the SDK
exclusively — no direct manager imports.

Run with::

    python -m case_studies.fraud_detection.app
"""

from __future__ import annotations

import os
from pathlib import Path

from mlops_framework.sdk import MLOpsProject, NotFoundError

from case_studies.fraud_detection import data
from case_studies.fraud_detection.pipelines import (
    train_advanced,
    train_baseline,
)


PROJECT_NAME = "fraud-detection"
DATASET_NAME = "credit-card-transactions"
MODEL_NAME = "fraud-xgboost"


def build_project() -> MLOpsProject:
    """Build an MLOpsProject with default adapters.

    The framework wires the local orchestrator and in-memory tracker for
    us, so the case study only imports the SDK.
    """
    project = MLOpsProject.with_defaults(PROJECT_NAME)
    # Register the pipelines the app will use
    project.register_pipeline(
        "fraud-baseline",
        "case_studies.fraud_detection.pipelines:train_baseline",
        description="Baseline fraud model",
    )
    project.register_pipeline(
        "fraud-advanced",
        "case_studies.fraud_detection.pipelines:train_advanced",
        description="Advanced fraud model with engineered features",
    )
    return project


def ensure_dataset(
    project: MLOpsProject,
    data_path: Path,
    n_rows: int = 5000,
) -> None:
    """Create the dataset (if missing) and add a version pointing at ``data_path``."""
    try:
        project.get_dataset(DATASET_NAME)
    except NotFoundError:
        project.create_dataset(DATASET_NAME, description="Synthetic credit card transactions")

    ds = project.get_dataset(DATASET_NAME)
    if not any(v.storage_uri == str(data_path) for v in ds.versions):
        ds.create_version(
            storage_uri=str(data_path),
            row_count=n_rows,
            metadata=data.schema_metadata(),
        )


def ensure_model(project: MLOpsProject) -> None:
    """Create the fraud-detection model if it doesn't exist yet."""
    try:
        project.get_model(MODEL_NAME)
    except NotFoundError:
        project.create_model(
            MODEL_NAME,
            task="binary_classification",
            description="Fraud vs legit credit-card transactions",
        )


def run_full_lifecycle(
    project: MLOpsProject,
    dataset_version,
    pipeline: str = "fraud-baseline",
) -> object:
    """Train the fraud model on ``dataset_version`` and return the run."""
    return project.train(
        dataset_version=dataset_version,
        pipeline=pipeline,
        parameters={"seed": 42},
        wait=True,
        timeout=30,
    )


def main() -> None:
    """CLI entry point — run the full Fraud Detection lifecycle."""
    here = Path(__file__).parent
    data_path = here / "data" / "transactions.csv"
    if not data_path.exists():
        data.write_csv(data_path, n_rows=5000)

    project = build_project()
    try:
        ensure_dataset(project, data_path, n_rows=5000)
        ensure_model(project)

        ds = project.get_dataset(DATASET_NAME)
        v = ds.latest_version
        assert v is not None
        run = run_full_lifecycle(project, v)
        print(
            f"Run {run.id} finished with status={run.status}; "
            f"pipeline_id={run.pipeline_id}"
        )

        # Lineage
        graph = project.lineage.for_dataset_version(v.id)
        kinds = {n["type"] for n in graph["nodes"]}
        print(f"Lineage node types: {sorted(kinds)}")
    finally:
        project.orchestrator.shutdown()


if __name__ == "__main__":
    main()
