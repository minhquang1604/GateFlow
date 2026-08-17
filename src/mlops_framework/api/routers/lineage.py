"""Lineage router — expose LineageManager graphs as JSON."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.schemas import LineageGraphOut
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model_version import ModelVersion
from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.lineage.manager import LineageManager

router = APIRouter()


@router.get("/lineage/dataset/{dataset_id}", response_model=LineageGraphOut)
def dataset_lineage(
    dataset_id: int,
    db: Session = Depends(get_db),
) -> LineageGraphOut:
    """Return the whole-family lineage graph for a ``Dataset`` — every
    version it has, side by side, not just one. See
    ``LineageManager.graph_for_dataset``."""
    if db.get(Dataset, dataset_id) is None:
        raise HTTPException(
            status_code=404, detail=f"Dataset {dataset_id} not found"
        )
    graph = LineageManager(db).graph_for_dataset(dataset_id)
    return LineageGraphOut.from_graph(graph)


@router.get(
    "/lineage/dataset-version/{version_id}", response_model=LineageGraphOut
)
def dataset_version_lineage(
    version_id: int,
    db: Session = Depends(get_db),
) -> LineageGraphOut:
    """Return the lineage graph rooted at a ``DatasetVersion``."""
    if db.get(DatasetVersion, version_id) is None:
        raise HTTPException(
            status_code=404, detail=f"DatasetVersion {version_id} not found"
        )
    graph = LineageManager(db).graph_for_dataset_version(version_id)
    return LineageGraphOut.from_graph(graph)


@router.get(
    "/lineage/model-version/{version_id}", response_model=LineageGraphOut
)
def model_version_lineage(
    version_id: int,
    db: Session = Depends(get_db),
) -> LineageGraphOut:
    """Return the lineage graph rooted at a ``ModelVersion``."""
    if db.get(ModelVersion, version_id) is None:
        raise HTTPException(
            status_code=404, detail=f"ModelVersion {version_id} not found"
        )
    graph = LineageManager(db).graph_for_model_version(version_id)
    return LineageGraphOut.from_graph(graph)


@router.get(
    "/lineage/training-run/{run_id}", response_model=LineageGraphOut
)
def training_run_lineage(
    run_id: int,
    db: Session = Depends(get_db),
) -> LineageGraphOut:
    """Return the lineage graph rooted at a ``TrainingRun``."""
    if db.get(TrainingRun, run_id) is None:
        raise HTTPException(
            status_code=404, detail=f"TrainingRun {run_id} not found"
        )
    graph = LineageManager(db).graph_for_training_run(run_id)
    return LineageGraphOut.from_graph(graph)
