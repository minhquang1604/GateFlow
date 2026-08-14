"""Drift router — read the latest drift evaluation, or ask for a new one.

The read half is a plain lookup. The write half exists because drift
could otherwise only be evaluated from Python: the framework
deliberately does not read dataset files (``DriftService`` takes feature
values from its caller, and nothing in ``src/`` opens an S3 object or a
CSV), so neither a console button nor this process can compute drift
itself. ``POST /drift/{id}/check`` therefore triggers an Airflow DAG,
which reads the data where the data already is and posts the values
back to ``POST /internal/drift``. See that endpoint and
``trigger_drift_check`` below.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_actor, get_audit_manager, get_db
from mlops_framework.api.schemas import DriftEvaluationOut
from mlops_framework.api.security import require_write_token
from mlops_framework.audit.manager import AuditManager
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


# ---------------------------------------------------------------------- #
# POST /drift/{version_id}/check — trigger a drift run
# ---------------------------------------------------------------------- #

DRIFT_DAG_ID = "mlops_drift_check"


class DriftCheckRequest(BaseModel):
    # Defaults to the previous version of the same dataset, which is the
    # comparison a reader almost always means by "has this drifted".
    reference_version_id: int | None = None
    # Rows sampled per feature. A KS test settles on a few thousand
    # points; the default keeps the DAG's callback payload small enough
    # to be an ordinary HTTP request rather than a bulk transfer.
    sample_size: int = Field(default=5000, ge=100, le=100_000)


class DriftCheckResponse(BaseModel):
    execution_id: str
    dag_id: str
    reference_dataset_version_id: int
    current_dataset_version_id: int


@router.post(
    "/drift/{version_id}/check",
    response_model=DriftCheckResponse,
    status_code=202,
    dependencies=[Depends(require_write_token)],
)
def trigger_drift_check(
    version_id: int,
    request: DriftCheckRequest,
    db: Session = Depends(get_db),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> DriftCheckResponse:
    """Ask Airflow to run a drift check, and return once it is queued.

    Drift could previously only be evaluated from Python, because the
    framework deliberately does not read dataset files — ``DriftService``
    takes feature values from its caller, and nothing in ``src/`` opens
    an S3 object or a CSV. A console button therefore cannot compute
    drift itself, and neither can this endpoint.

    So the work goes where the data already is. Airflow reads datasets
    for training today; ``mlops_drift_check`` reads the two versions,
    samples them, and posts the values back to ``POST /internal/drift``,
    which is where the detector, the thresholds and the verdict live.
    The app container gains no S3 credentials and never holds a dataset
    in a request handler — a 144 MB CSV inside a 256 MiB reservation is
    exactly the shape of failure that killed Airflow's own gunicorn
    worker before (see ``orchestration/airflow``'s module docstring).

    202, not 200: this returns when the DAG run is *queued*. The verdict
    lands on ``GET /drift/{version_id}`` when the DAG finishes, the same
    way a training run's result does.
    """
    current = db.get(DatasetVersion, version_id)
    if current is None:
        raise HTTPException(
            status_code=404, detail=f"DatasetVersion {version_id} not found"
        )

    if request.reference_version_id is not None:
        reference = db.get(DatasetVersion, request.reference_version_id)
        if reference is None:
            raise HTTPException(
                status_code=404,
                detail=f"DatasetVersion {request.reference_version_id} not found",
            )
        if reference.id == current.id:
            raise HTTPException(
                status_code=422,
                detail="a version cannot be compared against itself",
            )
    else:
        reference = db.execute(
            select(DatasetVersion)
            .where(
                DatasetVersion.dataset_id == current.dataset_id,
                DatasetVersion.version_number < current.version_number,
            )
            .order_by(DatasetVersion.version_number.desc())
            .limit(1)
        ).scalars().first()
        if reference is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"DatasetVersion {version_id} is the first version of its "
                    "dataset — there is nothing to compare it against. Pass "
                    "reference_version_id explicitly to compare across datasets."
                ),
            )

    base_url = os.environ.get("AIRFLOW_BASE_URL")
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail="AIRFLOW_BASE_URL is not configured on this deployment",
        )

    from mlops_framework.orchestration.airflow import AirflowOrchestrator

    try:
        with AirflowOrchestrator(
            base_url=base_url,
            username=os.environ.get("AIRFLOW_USERNAME", "airflow"),
            password=os.environ.get("AIRFLOW_PASSWORD", "airflow"),
        ) as orchestrator:
            execution_id = orchestrator.trigger_pipeline(
                DRIFT_DAG_ID,
                {
                    "reference_version_id": reference.id,
                    "current_version_id": current.id,
                    "sample_size": request.sample_size,
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"could not trigger {DRIFT_DAG_ID}: {exc}"
        ) from exc

    am.record(
        actor=actor,
        action="DRIFT_CHECK_TRIGGERED",
        entity_type="DatasetVersion",
        entity_id=current.id,
        metadata={
            "reference_dataset_version_id": reference.id,
            "execution_id": execution_id,
            "sample_size": request.sample_size,
        },
    )
    return DriftCheckResponse(
        execution_id=execution_id,
        dag_id=DRIFT_DAG_ID,
        reference_dataset_version_id=reference.id,
        current_dataset_version_id=current.id,
    )
