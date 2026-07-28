"""MLOps Framework SDK.

A small Python façade that hides the managers, the orchestrator, and the
tracker behind a single ``MLOpsProject`` object. Designed so app developers
never need to import a manager directly.

Example::

    from mlops_framework import MLOpsProject

    project = MLOpsProject("fraud-detection")
    project.register_pipeline(
        "xgboost-training", "my_pkg.pipelines:train_xgb"
    )

    dataset = project.create_dataset("transactions")
    version = dataset.create_version(
        storage_uri="s3://bucket/v1.parquet",
        row_count=100_000,
    )
    run = project.train(dataset_version=version, pipeline="xgboost-training")
    print(run.status, run.metrics)
"""

from mlops_framework.sdk.exceptions import (
    AlreadyExistsError,
    GovernanceError,
    MLOpsError,
    NotFoundError,
    PipelineNotRegisteredError,
    TrainingError,
)
from mlops_framework.sdk.project import (
    MLOpsDataset,
    MLOpsDatasetVersion,
    MLOpsLineage,
    MLOpsModel,
    MLOpsModelVersion,
    MLOpsProject,
    MLOpsRun,
)

__all__ = [
    "MLOpsProject",
    "MLOpsDataset",
    "MLOpsDatasetVersion",
    "MLOpsRun",
    "MLOpsModel",
    "MLOpsModelVersion",
    "MLOpsLineage",
    "MLOpsError",
    "NotFoundError",
    "AlreadyExistsError",
    "PipelineNotRegisteredError",
    "TrainingError",
    "GovernanceError",
]
