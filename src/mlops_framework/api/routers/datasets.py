"""Datasets router — list, detail, and per-version endpoints.

``GET /datasets`` is paged and answers in a fixed number of queries.
Both were needed: it listed *every* dataset with no bound, and then ran
two more queries per row to fill in ``version_count`` and
``latest_version`` — 2N+1 in total, on the console's landing path for
that page. The counts and the latest versions now come from one grouped
query each, so the cost is three queries whatever the page holds.

Paging follows ``runs.py``'s convention (``limit``/``offset``, same
bounds) rather than inventing a second one, and the unpaged total comes
back in the ``X-Total-Count`` header so the response body stays the
plain list every existing caller already parses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_dataset_manager, get_db
from mlops_framework.api.schemas import (
    DatasetOut,
    DatasetVersionOut,
)
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
)

router = APIRouter()


@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(
    response: Response,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[DatasetOut]:
    """List datasets with their version count and latest version.

    Ordered by id so paging is stable. ``X-Total-Count`` carries the
    unpaged total — see the module docstring.
    """
    total = db.execute(select(func.count(Dataset.id))).scalar_one()
    response.headers["X-Total-Count"] = str(total)

    datasets = list(
        db.execute(
            select(Dataset).order_by(Dataset.id).limit(limit).offset(offset)
        ).scalars().all()
    )
    if not datasets:
        return []

    ids = [ds.id for ds in datasets]
    counts = dict(
        db.execute(
            select(DatasetVersion.dataset_id, func.count(DatasetVersion.id))
            .where(DatasetVersion.dataset_id.in_(ids))
            .group_by(DatasetVersion.dataset_id)
        ).all()
    )
    # The latest version per dataset, in one round trip: join each row
    # back against its own group's max(version_number). A plain
    # "order by version_number desc limit 1" cannot express "per
    # dataset", and pulling every version to pick in Python would move
    # the N+1 into memory rather than remove it.
    newest = (
        select(
            DatasetVersion.dataset_id.label("dataset_id"),
            func.max(DatasetVersion.version_number).label("version_number"),
        )
        .where(DatasetVersion.dataset_id.in_(ids))
        .group_by(DatasetVersion.dataset_id)
        .subquery()
    )
    latest_by_dataset = {
        v.dataset_id: v
        for v in db.execute(
            select(DatasetVersion).join(
                newest,
                (DatasetVersion.dataset_id == newest.c.dataset_id)
                & (DatasetVersion.version_number == newest.c.version_number),
            )
        ).scalars().all()
    }

    return [
        DatasetOut(
            id=ds.id,
            name=ds.name,
            description=ds.description,
            version_count=counts.get(ds.id, 0),
            latest_version=(
                DatasetVersionOut.from_orm_with_metadata(latest_by_dataset[ds.id])
                if ds.id in latest_by_dataset
                else None
            ),
        )
        for ds in datasets
    ]


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
