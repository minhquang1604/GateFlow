"""Drift router — return the latest drift evaluation for a dataset version."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.schemas import DriftEvaluationOut
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import DriftEvaluation

router = APIRouter()


@router.get("/drift/{version_id}", response_model=DriftEvaluationOut | None)
def get_latest_drift(
    version_id: int,
    db: Session = Depends(get_db),
) -> DriftEvaluationOut | None:
    """Return the most recent drift evaluation involving a dataset version.

    A version can appear on either side of a comparison (reference or
    current — see :class:`DriftEvaluation`). Returns ``null`` (HTTP 200)
    when no evaluation has been run yet, same convention as
    ``GET /readiness/{version_id}``.
    """
    if db.get(DatasetVersion, version_id) is None:
        raise HTTPException(
            status_code=404, detail=f"DatasetVersion {version_id} not found"
        )
    eval_ = db.execute(
        select(DriftEvaluation)
        .where(
            or_(
                DriftEvaluation.reference_dataset_version_id == version_id,
                DriftEvaluation.current_dataset_version_id == version_id,
            )
        )
        .order_by(DriftEvaluation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if eval_ is None:
        return None
    return DriftEvaluationOut.from_orm_with_json(eval_)
