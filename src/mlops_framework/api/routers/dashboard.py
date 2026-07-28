"""Dashboard endpoint — aggregate counts for the Management UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.schemas import DashboardCounts
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.training_run import (
    RunStatus,
    TrainingRun,
)

router = APIRouter()


@router.get("/dashboard", response_model=DashboardCounts)
def get_dashboard(db: Session = Depends(get_db)) -> DashboardCounts:
    """Return aggregate counts for the Management UI dashboard.

    Counts are computed in a small number of SQL aggregates so the dashboard
    stays cheap to render, even with many rows.
    """
    datasets = db.execute(select(func.count(Dataset.id))).scalar_one()
    dataset_versions = db.execute(
        select(func.count(DatasetVersion.id))
    ).scalar_one()
    models = db.execute(select(func.count(ModelRow.id))).scalar_one()

    total_runs = db.execute(select(func.count(TrainingRun.id))).scalar_one()
    active_runs = db.execute(
        select(func.count(TrainingRun.id)).where(
            TrainingRun.status.in_([RunStatus.PENDING.value, RunStatus.RUNNING.value])
        )
    ).scalar_one()
    success_runs = db.execute(
        select(func.count(TrainingRun.id)).where(
            TrainingRun.status == RunStatus.SUCCESS.value
        )
    ).scalar_one()
    failed_runs = db.execute(
        select(func.count(TrainingRun.id)).where(
            TrainingRun.status == RunStatus.FAILED.value
        )
    ).scalar_one()

    production_models = db.execute(
        select(func.count(func.distinct(ModelVersion.model_id))).where(
            ModelVersion.state == ModelState.PRODUCTION.value
        )
    ).scalar_one()

    completed = success_runs + failed_runs
    success_rate = (success_runs / completed) if completed else 0.0

    return DashboardCounts(
        datasets=datasets,
        dataset_versions=dataset_versions,
        total_runs=total_runs,
        active_runs=active_runs,
        success_runs=success_runs,
        failed_runs=failed_runs,
        models=models,
        production_models=production_models,
        success_rate=success_rate,
    )
