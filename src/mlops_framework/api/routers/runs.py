"""Training runs router."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.schemas import TrainingRunOut
from mlops_framework.database.models.training_run import TrainingRun

router = APIRouter()


@router.get("/training-runs", response_model=list[TrainingRunOut])
def list_runs(
    status: Optional[str] = Query(default=None, description="Filter by run status"),
    dataset_version_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[TrainingRunOut]:
    """List training runs, newest first.

    Supports filtering by ``status`` and ``dataset_version_id``.
    """
    stmt = select(TrainingRun).order_by(TrainingRun.id.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(TrainingRun.status == status)
    if dataset_version_id is not None:
        stmt = stmt.where(TrainingRun.dataset_version_id == dataset_version_id)
    runs = list(db.execute(stmt).scalars().all())
    return [TrainingRunOut.from_orm_with_json(r) for r in runs]


@router.get("/training-runs/{run_id}", response_model=TrainingRunOut)
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
) -> TrainingRunOut:
    """Return a single training run by id."""
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    return TrainingRunOut.from_orm_with_json(run)
