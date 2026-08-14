"""Models router — list, detail, and per-version endpoints.

``GET /models`` is paged and answers in a fixed number of queries, for
the same reasons ``datasets.py``'s list endpoint is — see that module's
docstring. Its per-row work was ``list_model_versions`` followed by a
scan for the PRODUCTION one; both now come from one grouped query and
one filtered query over the page's ids.

Fetching production versions in a single ``state == PRODUCTION`` query
relies on there being at most one per model, which migration
``006_one_production_per_model`` enforces with a partial unique index —
not on it merely being true in practice today.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db, get_model_manager
from mlops_framework.api.schemas import (
    ModelOut,
    ModelVersionOut,
)
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.exceptions import ModelNotFoundError, ModelVersionNotFoundError
from mlops_framework.model.manager import ModelManager
from mlops_framework.sdk.report import build_report

router = APIRouter()


@router.get("/models", response_model=list[ModelOut])
def list_models(
    response: Response,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ModelOut]:
    """List models with version count and production version summary.

    Ordered by id so paging is stable. ``X-Total-Count`` carries the
    unpaged total — see the module docstring.
    """
    total = db.execute(select(func.count(ModelRow.id))).scalar_one()
    response.headers["X-Total-Count"] = str(total)

    models = list(
        db.execute(
            select(ModelRow).order_by(ModelRow.id).limit(limit).offset(offset)
        ).scalars().all()
    )
    if not models:
        return []

    ids = [m.id for m in models]
    counts = dict(
        db.execute(
            select(ModelVersion.model_id, func.count(ModelVersion.id))
            .where(ModelVersion.model_id.in_(ids))
            .group_by(ModelVersion.model_id)
        ).all()
    )
    production_by_model = {
        v.model_id: v
        for v in db.execute(
            select(ModelVersion).where(
                ModelVersion.model_id.in_(ids),
                ModelVersion.state == ModelState.PRODUCTION.value,
            )
        ).scalars().all()
    }

    return [
        ModelOut(
            id=m.id,
            name=m.name,
            description=m.description,
            task=m.task,
            version_count=counts.get(m.id, 0),
            production_version=(
                ModelVersionOut.from_orm_with_metrics(production_by_model[m.id])
                if m.id in production_by_model
                else None
            ),
        )
        for m in models
    ]


@router.get("/models/{model_id}", response_model=ModelOut)
def get_model(
    model_id: int,
    db: Session = Depends(get_db),
    mm: ModelManager = Depends(get_model_manager),
) -> ModelOut:
    """Return a single model with version_count and production version."""
    try:
        m = mm.get_model(model_id)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    versions = mm.list_model_versions(m.id)
    prod = next(
        (v for v in versions if v.state == ModelState.PRODUCTION.value), None
    )
    return ModelOut(
        id=m.id,
        name=m.name,
        description=m.description,
        task=m.task,
        version_count=len(versions),
        production_version=(
            ModelVersionOut.from_orm_with_metrics(prod) if prod is not None else None
        ),
    )


@router.get(
    "/models/{model_id}/versions",
    response_model=list[ModelVersionOut],
)
def list_model_versions(
    model_id: int,
    db: Session = Depends(get_db),
    mm: ModelManager = Depends(get_model_manager),
) -> list[ModelVersionOut]:
    """List all versions of a model, ordered by version_number."""
    try:
        mm.get_model(model_id)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    versions = sorted(
        mm.list_model_versions(model_id), key=lambda v: v.version_number
    )
    return [ModelVersionOut.from_orm_with_metrics(v) for v in versions]


@router.get(
    "/model-versions/{version_id}", response_model=ModelVersionOut
)
def get_model_version(
    version_id: int,
    db: Session = Depends(get_db),
) -> ModelVersionOut:
    """Return a single model version by id."""
    mv = db.get(ModelVersion, version_id)
    if mv is None:
        raise HTTPException(
            status_code=404, detail=f"ModelVersion {version_id} not found"
        )
    return ModelVersionOut.from_orm_with_metrics(mv)


@router.get("/model-versions/{version_id}/report")
def get_model_version_report(
    version_id: int,
    format: str = Query(default="markdown", pattern="^(markdown|html)$"),
    db: Session = Depends(get_db),
) -> Response:
    """Download a self-contained reproducibility report for one ModelVersion.

    Thin wrapper over ``sdk/report.py::build_report`` — the same
    function ``MLOpsProject.report()`` calls — so Gateflow's "Download
    report" button (``model_detail.html``) works without a Python
    process running the SDK. ``Content-Disposition: attachment`` makes
    the browser save it rather than navigate to it.
    """
    try:
        content = build_report(db, version_id, format=format)
    except ModelVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if format == "html":
        media_type, ext = "text/html", "html"
    else:
        media_type, ext = "text/markdown", "md"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="model-version-{version_id}-report.{ext}"'
            )
        },
    )
