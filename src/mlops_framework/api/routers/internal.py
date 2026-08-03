"""Internal endpoints for the Airflow DAG.

The framework's Airflow image no longer installs ``mlops_framework``
directly (see ``infrastructure/airflow/Dockerfile`` — Airflow 2.10.4
pins SQLAlchemy 1.4.x internally, which conflicts with the framework's
own ``sqlalchemy>=2.0.0`` requirement). ``infrastructure/airflow/dags/
mlops_training_pipeline.py`` calls these endpoints over HTTP instead of
importing the framework's ORM models and managers directly.

These routes intentionally do the same work
``mlops_training_pipeline.py`` used to do in-process: resolve a
TrainingRun + its DatasetVersion, and apply the model promotion
policy after a training run completes.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db, get_model_manager
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.governance.promotion import (
    ModelPromotionPolicy,
    PromotionConfig,
    PromotionContext,
)
from mlops_framework.model.manager import ModelManager

router = APIRouter()


# ---------------------------------------------------------------------- #
# GET /internal/training-runs/{run_id}/context
# ---------------------------------------------------------------------- #


class TrainingRunContextOut(BaseModel):
    training_run_id: int
    dataset_version_id: int
    storage_uri: str
    row_count: int
    pipeline_id: Optional[str] = None
    metadata: dict[str, Any] = {}


@router.get(
    "/internal/training-runs/{run_id}/context",
    response_model=TrainingRunContextOut,
)
def get_training_run_context(
    run_id: int,
    db: Session = Depends(get_db),
) -> TrainingRunContextOut:
    """Resolve a TrainingRun + its DatasetVersion for the Airflow DAG.

    Replaces the DAG's old in-process ``resolve_context`` task, which
    imported ``TrainingRun``/``DatasetVersion`` directly.
    """
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    dataset_version = db.get(DatasetVersion, run.dataset_version_id)
    if dataset_version is None:
        raise HTTPException(
            status_code=404,
            detail=f"DatasetVersion {run.dataset_version_id} not found",
        )
    return TrainingRunContextOut(
        training_run_id=run.id,
        dataset_version_id=dataset_version.id,
        storage_uri=dataset_version.storage_uri,
        row_count=dataset_version.row_count,
        pipeline_id=run.pipeline_id,
        metadata=json.loads(run.metadata_json or "{}"),
    )


# ---------------------------------------------------------------------- #
# POST /internal/models/{model_name}/promote
# ---------------------------------------------------------------------- #


class PromoteModelRequest(BaseModel):
    dataset_version_id: int
    training_run_id: int
    mlflow_run_id: Optional[str] = None
    metrics: dict[str, Any] = {}
    artifact_uri: Optional[str] = None
    min_f1: float = 0.0


class PromoteModelResponse(BaseModel):
    promoted: bool
    model_version_id: int
    model_version: Optional[int] = None
    reasons: list[str] = []


@router.post(
    "/internal/models/{model_name}/promote",
    response_model=PromoteModelResponse,
)
def promote_model(
    model_name: str,
    request: PromoteModelRequest,
    db: Session = Depends(get_db),
    mm: ModelManager = Depends(get_model_manager),
) -> PromoteModelResponse:
    """Create a CANDIDATE model version and apply the promotion policy.

    Replaces the DAG's old in-process ``register_and_promote`` task.
    The model row must already exist (created by the demo / app code);
    this endpoint fails loudly otherwise, same as the original task.
    """
    model_row = mm.get_model_by_name(model_name)
    if model_row is None:
        raise HTTPException(
            status_code=404, detail=f"Model {model_name!r} not registered"
        )

    candidate = mm.create_model_version(
        model_id=model_row.id,
        dataset_version_id=request.dataset_version_id,
        training_run_id=request.training_run_id,
        mlflow_run_id=request.mlflow_run_id,
        state=ModelState.CANDIDATE,
        metrics=request.metrics,
        artifact_uri=request.artifact_uri,
    )

    production = db.execute(
        select(ModelVersion)
        .where(
            ModelVersion.model_id == model_row.id,
            ModelVersion.state == ModelState.PRODUCTION,
        )
        .limit(1)
    ).scalars().first()

    decision = ModelPromotionPolicy().evaluate(
        context=PromotionContext(candidate=candidate, production=production),
        config=PromotionConfig(
            min_metrics={"f1": request.min_f1},
            must_beat_production=False,
            allow_cold_start=True,
        ),
    )
    if not decision.approved:
        mm.transition_state(candidate.id, ModelState.REJECTED)
        return PromoteModelResponse(
            promoted=False,
            model_version_id=candidate.id,
            reasons=decision.reasons,
        )

    mm.transition_state(candidate.id, ModelState.APPROVED)
    mm.transition_state(candidate.id, ModelState.PRODUCTION)
    if production is not None and production.id != candidate.id:
        mm.transition_state(production.id, ModelState.ARCHIVED)

    return PromoteModelResponse(
        promoted=True,
        model_version_id=candidate.id,
        model_version=candidate.version_number,
    )
