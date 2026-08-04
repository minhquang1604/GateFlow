"""Training runs router.

Beyond the framework's own rows, two endpoints here reach out to the
systems a run was executed on — Airflow for per-task state, MLflow for
step-wise metric history. Both are strictly best-effort: the UI renders
without them, so a tracking server being down degrades a panel rather
than failing the page.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.schemas import TrainingRunOut
from mlops_framework.config.settings import get_settings
from mlops_framework.database.models.training_run import RunStatus, TrainingRun

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


# ---------------------------------------------------------------------- #
# External-system views. Best-effort by design — see the module docstring.
# ---------------------------------------------------------------------- #


class ExternalPanel(BaseModel):
    """A panel the UI renders only when the backing system answered.

    ``available`` false plus ``reason`` lets the page say *why* a panel is
    empty ("no Airflow configured") instead of rendering a blank box that
    looks like a bug.
    """

    available: bool
    reason: Optional[str] = None
    data: Any = None


def _run_or_404(db: Session, run_id: int) -> TrainingRun:
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    return run


@router.get("/training-runs/{run_id}/tasks", response_model=ExternalPanel)
def get_run_tasks(run_id: int, db: Session = Depends(get_db)) -> ExternalPanel:
    """Per-task state for a run that was executed on Airflow.

    Returns ``{task_id: state}``, the same shape Airflow's grid view is
    built on.
    """
    run = _run_or_404(db, run_id)
    metadata = json.loads(run.metadata_json or "{}")
    execution_id = metadata.get("orchestrator_execution_id")
    if not execution_id:
        return ExternalPanel(available=False, reason="run has no orchestrator execution id")
    if "/" not in str(execution_id):
        return ExternalPanel(
            available=False,
            reason="run was not executed on Airflow (local orchestrator)",
        )

    settings = get_settings()
    if not settings.airflow_base_url:
        return ExternalPanel(available=False, reason="AIRFLOW_BASE_URL is not configured")

    from mlops_framework.orchestration.airflow import AirflowOrchestrator

    try:
        with AirflowOrchestrator(
            base_url=settings.airflow_base_url,
            username=settings.airflow_username,
            password=settings.airflow_password,
        ) as orchestrator:
            states = orchestrator.get_task_instance_states(str(execution_id))
    except Exception as exc:  # noqa: BLE001 - never fail the page
        return ExternalPanel(available=False, reason=f"Airflow unreachable: {exc}")

    return ExternalPanel(
        available=True,
        data={"execution_id": execution_id, "tasks": states},
    )


@router.get("/training-runs/{run_id}/metric-history", response_model=ExternalPanel)
def get_run_metric_history(run_id: int, db: Session = Depends(get_db)) -> ExternalPanel:
    """Step-wise metric history from MLflow, for the run's charts.

    The framework stores only each metric's final value; MLflow keeps the
    whole series, which is what makes a training curve renderable.
    """
    run = _run_or_404(db, run_id)
    if not run.mlflow_run_id:
        return ExternalPanel(available=False, reason="run has no MLflow run id")

    settings = get_settings()
    if not settings.mlflow_tracking_uri:
        return ExternalPanel(
            available=False, reason="MLFLOW_TRACKING_URI is not configured"
        )

    try:
        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=settings.mlflow_tracking_uri)
        mlflow_run = client.get_run(run.mlflow_run_id)
        series = {
            key: [
                {"step": m.step, "value": m.value, "timestamp": m.timestamp}
                for m in client.get_metric_history(run.mlflow_run_id, key)
            ]
            for key in mlflow_run.data.metrics
        }
    except Exception as exc:  # noqa: BLE001 - never fail the page
        return ExternalPanel(available=False, reason=f"MLflow unavailable: {exc}")

    return ExternalPanel(
        available=True,
        data={
            "mlflow_run_id": run.mlflow_run_id,
            "tracking_uri": settings.mlflow_tracking_uri,
            "params": dict(mlflow_run.data.params),
            "metrics": dict(mlflow_run.data.metrics),
            "history": series,
        },
    )
