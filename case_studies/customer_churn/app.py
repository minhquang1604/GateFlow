"""App entry point for the Customer Churn case study.

Uses **only** the public SDK. The framework wires the local orchestrator
and in-memory tracker for us.
"""

from __future__ import annotations

from pathlib import Path

from mlops_framework.sdk import MLOpsProject, NotFoundError

from case_studies.customer_churn import data
from case_studies.customer_churn.pipelines import train_balanced, train_baseline


PROJECT_NAME = "customer-churn"
DATASET_NAME = "telco-customers"
MODEL_NAME = "churn-classifier"


def build_project() -> MLOpsProject:
    project = MLOpsProject.with_defaults(PROJECT_NAME)
    project.register_pipeline(
        "churn-baseline",
        "case_studies.customer_churn.pipelines:train_baseline",
        description="Baseline churn classifier",
    )
    project.register_pipeline(
        "churn-balanced",
        "case_studies.customer_churn.pipelines:train_balanced",
        description="Class-balanced churn classifier",
    )
    return project


def ensure_dataset(project: MLOpsProject, data_path: Path, n_rows: int = 3000) -> None:
    try:
        project.get_dataset(DATASET_NAME)
    except NotFoundError:
        project.create_dataset(
            DATASET_NAME,
            description="Synthetic telecom customer records",
        )
    ds = project.get_dataset(DATASET_NAME)
    if not any(v.storage_uri == str(data_path) for v in ds.versions):
        ds.create_version(
            storage_uri=str(data_path),
            row_count=n_rows,
            metadata=data.schema_metadata(),
        )


def ensure_model(project: MLOpsProject) -> None:
    try:
        project.get_model(MODEL_NAME)
    except NotFoundError:
        project.create_model(
            MODEL_NAME,
            task="binary_classification",
            description="Predicts 30-day churn",
        )


def run_full_lifecycle(project: MLOpsProject, dataset_version, pipeline: str = "churn-baseline"):
    return project.train(
        dataset_version=dataset_version,
        pipeline=pipeline,
        parameters={"seed": 7},
        wait=True,
        timeout=30,
    )


def main() -> None:
    here = Path(__file__).parent
    data_path = here / "data" / "customers.csv"
    if not data_path.exists():
        data.write_csv(data_path, n_rows=3000)

    project = build_project()
    try:
        ensure_dataset(project, data_path, n_rows=3000)
        ensure_model(project)

        ds = project.get_dataset(DATASET_NAME)
        v = ds.latest_version
        assert v is not None
        run = run_full_lifecycle(project, v)
        print(
            f"Run {run.id} finished with status={run.status}; "
            f"pipeline_id={run.pipeline_id}"
        )

        graph = project.lineage.for_dataset_version(v.id)
        kinds = {n["type"] for n in graph["nodes"]}
        print(f"Lineage node types: {sorted(kinds)}")
    finally:
        project.orchestrator.shutdown()


if __name__ == "__main__":
    main()
