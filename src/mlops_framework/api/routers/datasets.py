"""Datasets router — list, detail, and per-version endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_dataset_manager, get_db
from mlops_framework.api.schemas import (
    DatasetOut,
    DatasetVersionOut,
)
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
)

router = APIRouter()


@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(
    db: Session = Depends(get_db),
    dm: DatasetManager = Depends(get_dataset_manager),
) -> list[DatasetOut]:
    """List all datasets with their version count and latest version."""
    datasets = dm.list_datasets()
    out: list[DatasetOut] = []
    for ds in datasets:
        versions = dm.list_versions(ds.id)
        latest = dm.get_latest_version(ds.id)
        out.append(
            DatasetOut(
                id=ds.id,
                name=ds.name,
                description=ds.description,
                version_count=len(versions),
                latest_version=(
                    DatasetVersionOut.from_orm_with_metadata(latest)
                    if latest is not None
                    else None
                ),
            )
        )
    return out


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    dm: DatasetManager = Depends(get_dataset_manager),
) -> DatasetOut:
    """Return a single dataset with all its versions."""
    try:
        ds = dm.get_dataset(dataset_id)
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    versions = dm.list_versions(ds.id)
    latest = dm.get_latest_version(ds.id)
    return DatasetOut(
        id=ds.id,
        name=ds.name,
        description=ds.description,
        version_count=len(versions),
        latest_version=(
            DatasetVersionOut.from_orm_with_metadata(latest)
            if latest is not None
            else None
        ),
    )


@router.get(
    "/datasets/{dataset_id}/versions",
    response_model=list[DatasetVersionOut],
)
def list_dataset_versions(
    dataset_id: int,
    db: Session = Depends(get_db),
    dm: DatasetManager = Depends(get_dataset_manager),
) -> list[DatasetVersionOut]:
    """List all versions of a dataset, ordered by version_number."""
    try:
        dm.get_dataset(dataset_id)
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        DatasetVersionOut.from_orm_with_metadata(v)
        for v in dm.list_versions(dataset_id)
    ]


@router.get("/dataset-versions/{version_id}", response_model=DatasetVersionOut)
def get_dataset_version(
    version_id: int,
    db: Session = Depends(get_db),
    dm: DatasetManager = Depends(get_dataset_manager),
) -> DatasetVersionOut:
    """Return a single dataset version by id."""
    try:
        v = dm.get_version(version_id)
    except DatasetVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return DatasetVersionOut.from_orm_with_metadata(v)
