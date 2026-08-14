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

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import (
    get_audit_manager,
    get_db,
    get_model_manager,
)
from mlops_framework.api.schemas import (
    ModelOut,
    ModelVersionOut,
)
from mlops_framework.api.security import get_actor, require_write_token
from mlops_framework.audit.manager import AuditManager
from mlops_framework.config.settings import get_settings
from mlops_framework.database.models.governance_event import GovernanceEventSeverity
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.events.publisher import (
    HttpEventPublisher,
    ModelPromotedEvent,
    ModelRolledBackEvent,
)
from mlops_framework.events.store import GovernanceEventStore
from mlops_framework.exceptions import (
    ConcurrentPromotionError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    RollbackError,
)
from mlops_framework.model.manager import ModelManager
from mlops_framework.sdk.report import build_report
from mlops_framework.tracking import mlflow_registry as regsync

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


# ---------------------------------------------------------------------- #
# Rollback
# ---------------------------------------------------------------------- #


class RollbackResponse(BaseModel):
    model_id: int
    model_name: str
    restored_version_id: int
    restored_version: int
    previous_production_id: int | None = None
    previous_production_version: int | None = None
    # Whether the serving bridge acknowledged the reload. False means the
    # framework's registry has rolled back but whatever is serving may
    # not have — the operator needs to know which of those happened.
    serving_reloaded: bool = False


@router.post(
    "/model-versions/{version_id}/rollback",
    response_model=RollbackResponse,
    dependencies=[Depends(require_write_token)],
)
def rollback_model_version(
    version_id: int,
    db: Session = Depends(get_db),
    mm: ModelManager = Depends(get_model_manager),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> RollbackResponse:
    """Put a previously-retired version back into production.

    The recovery path the model registry did not have — see
    ``ModelManager.rollback_to``, which owns the state machine work and
    explains why the promotion policy is deliberately not consulted.

    This handler adds the three things that have to happen *around* that
    swap for it to be real:

    * an AuditLog row, because a rollback is an operator decision and
      "who put the old version back, and when" is the first question
      asked afterwards;
    * a CRITICAL GovernanceEvent, so it lands on Gateflow's Alerts tab —
      a rollback means production was wrong, which is not a quiet
      bookkeeping change;
    * a reload published to the ServingBridge, so what is actually being
      served follows the registry. Without this the endpoint would only
      rewrite rows and leave the bad model answering requests, which is
      the one outcome that would make the feature worse than useless.
      The reload is best-effort and its result is reported rather than
      raised: the framework's own database is the record of decision,
      and a bridge that is down must not leave the registry half-rolled
      back. ``serving_reloaded`` tells the caller which happened.

    MLflow's registry is synced too, on the same never-raises contract
    the promotion paths already use (see ``tracking/mlflow_registry``).
    """
    try:
        result = mm.rollback_to(version_id)
    except ModelVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrentPromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    restored = mm.get_model_version(result.restored_version_id)

    regsync.sync_production(
        result.model_name,
        regsync.version_for_run(result.model_name, restored.mlflow_run_id),
    )

    am.record(
        actor=actor,
        action="MODEL_ROLLED_BACK",
        entity_type="ModelVersion",
        entity_id=result.restored_version_id,
        metadata={
            "model_name": result.model_name,
            "restored_version": result.restored_version_number,
            "previous_production_version": result.previous_production_number,
        },
    )
    GovernanceEventStore(db).record(
        ModelRolledBackEvent(
            model_id=result.model_id,
            model_name=result.model_name,
            restored_version=result.restored_version_number,
            previous_version=result.previous_production_number,
            actor=actor,
        ),
        message=(
            f"{result.model_name}: rolled back to v"
            f"{result.restored_version_number}"
            + (
                f", retiring v{result.previous_production_number}"
                if result.previous_production_number is not None
                else " (no version was in production)"
            )
            + f" — by {actor}"
        ),
        severity=GovernanceEventSeverity.CRITICAL,
        entity_type="ModelVersion",
        entity_id=result.restored_version_id,
    )

    reloaded = _publish_serving_reload(result, restored)
    return RollbackResponse(
        model_id=result.model_id,
        model_name=result.model_name,
        restored_version_id=result.restored_version_id,
        restored_version=result.restored_version_number,
        previous_production_id=result.previous_production_id,
        previous_production_version=result.previous_production_number,
        serving_reloaded=reloaded,
    )


def _publish_serving_reload(result: Any, restored: ModelVersion) -> bool:
    """Tell the ServingBridge to load the restored version.

    Returns False (never raises) when no bridge is configured or it does
    not acknowledge — see the endpoint docstring on why that is reported
    rather than fatal.
    """
    url = get_settings().serving_bridge_url
    if not url:
        return False
    try:
        metrics = json.loads(restored.metrics_json) if restored.metrics_json else {}
    except (TypeError, ValueError):
        metrics = {}
    publisher = HttpEventPublisher(url.rstrip("/") + "/internal/model/reload")
    try:
        return publisher.publish(
            ModelPromotedEvent(
                model_name=result.model_name,
                model_version=result.restored_version_number,
                artifact_uri=restored.artifact_uri,
                metrics={
                    k: float(v)
                    for k, v in metrics.items()
                    if isinstance(v, (int, float))
                },
            )
        )
    except Exception:  # noqa: BLE001 - a bridge problem is not a rollback failure
        return False
    finally:
        publisher.close()
