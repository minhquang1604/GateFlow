"""Readiness router — return the latest readiness evaluation for a version."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.schemas import ReadinessEvaluationOut
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessEvaluation,
)

router = APIRouter()


@router.get(
    "/readiness/{version_id}", response_model=ReadinessEvaluationOut | None
)
def get_latest_readiness(
    version_id: int,
    db: Session = Depends(get_db),
) -> ReadinessEvaluationOut | None:
    """Return the most recent readiness evaluation for a dataset version.

    Returns ``null`` (HTTP 200) when no evaluation has been run yet.
    """
    if db.get(DatasetVersion, version_id) is None:
        raise HTTPException(
            status_code=404, detail=f"DatasetVersion {version_id} not found"
        )
    eval_ = db.execute(
        select(ReadinessEvaluation)
        .where(ReadinessEvaluation.dataset_version_id == version_id)
        .order_by(ReadinessEvaluation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if eval_ is None:
        return None
    return ReadinessEvaluationOut.from_orm_with_json(eval_)
